"""
TelegramBridge - интеграция Telegram бота с OpenAI Agents SDK
Объединяет nanobot (Telegram, память, скилы) и grid (агенты, оркестрация)
"""

import asyncio
import logging
import traceback
import html
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode
from telegram.request import HTTPXRequest

from core.unified_memory import UnifiedMemory
from core.memory_store import MemoryStore

# Опциональные импорты
try:
    from core.skills_integration import SkillsIntegration
    from nanobot.skills_loader import SkillsLoader
    SKILLS_AVAILABLE = True
except ImportError:
    SKILLS_AVAILABLE = False
    SkillsIntegration = None
    SkillsLoader = None

try:
    from core.agent_factory import AgentFactory
    AGENT_FACTORY_AVAILABLE = True
except ImportError:
    AGENT_FACTORY_AVAILABLE = False
    AgentFactory = None

try:
    from core.managers.container_manager import ContainerManager
except ImportError:
    ContainerManager = None

logger = logging.getLogger(__name__)

# Лимит длины одного сообщения в Telegram (API)
TELEGRAM_MAX_MESSAGE_LENGTH = 4096


@dataclass
class BridgeConfig:
    """Конфигурация TelegramBridge"""
    telegram_token: str
    workspace_path: Path
    persist_path: Path
    max_message_history: int = 15
    enable_transparency: bool = True
    show_tool_calls: bool = True
    allowed_users: Optional[list] = None  # None = все пользователи
    max_concurrent_tasks_per_user: int = 1
    progress_update_interval: float = 1.0
    proxy_url: Optional[str] = None  # например http://127.0.0.1:10809


class TelegramBridge:
    """
    Главный мост между Telegram и агентами

    Архитектура:
    - Telegram bot (python-telegram-bot) -> TelegramBridge
    - TelegramBridge -> AgentFactory -> OpenAI Agents
    - LiveTransparencyBroadcaster -> Telegram (обновления в реальном времени)
    - UnifiedMemory (контекст разговора), MemoryStore (SQLite — долгосрочная память)
    - SkillsIntegration (nanobot skills как grid tools)
    """

    def __init__(self, config: BridgeConfig):
        self.config = config

        # Telegram Application
        self.app: Optional[Application] = None

        # Основные компоненты (глобальные)
        self.skills: Optional[SkillsIntegration] = None
        # ВАЖНО: для Telegram нельзя иметь "глобальный" AgentFactory с cwd проекта/конфига.
        # Держим фабрики строго per-user, чтобы не было переключений working_directory.
        self.agent_factory: Optional[AgentFactory] = None  # legacy; не использовать для TG
        self.user_agent_factories: Dict[int, AgentFactory] = {}  # user_id -> AgentFactory

        # Изоляция пользователей: каждый user_id имеет свою память и workspace
        self.user_memories: Dict[int, UnifiedMemory] = {}  # user_id -> UnifiedMemory (deprecated)
        self.user_memory_stores: Dict[int, MemoryStore] = {}  # user_id -> MemoryStore (new SQLite)
        self.user_workspaces: Dict[int, Path] = {}  # user_id -> workspace_path

        # Состояние активных операций
        self.active_tasks: Dict[int, asyncio.Task] = {}  # chat_id -> task

        # Контекст для каждого чата (chat_id -> список сообщений)
        self.chat_contexts: Dict[int, list] = {}  # chat_id -> [{"role": "user/assistant", "content": str}]

        # Speech processor (lazy init)
        self._speech_processor = None

        # Статистика (для команды /status)
        self.stats = {
            "start_time": datetime.now(),
            "messages_processed": 0,
            "agents_launched": 0,
            "errors_handled": 0,
        }

        # Container Manager
        self.container_manager = None
        if ContainerManager:
            try:
                from core.config import Config
                # Load global config to check isolation settings
                cfg = Config(config_path="config.yaml")
                self.container_manager = ContainerManager(cfg)
                if self.container_manager.enabled:
                    logger.info("🐳 Container isolation enabled")
                else:
                    logger.info("ℹ️ Container isolation disabled in config")
            except Exception as e:
                logger.error(f"Failed to initialize ContainerManager: {e}")
        else:
            logger.warning("⚠️ ContainerManager class not found")

        logger.info("TelegramBridge инициализирован")

    def _get_user_agent_factory(self, user_id: int) -> Optional["AgentFactory"]:
        """
        Получить per-user AgentFactory с фиксированным working_directory:
        /home/daniil/grid/workspace/user_<id>

        Ключевое: никакого "set_working_directory туда/обратно".
        """
        if not AGENT_FACTORY_AVAILABLE:
            return None
        if user_id in self.user_agent_factories:
            return self.user_agent_factories[user_id]

        # Ensure dirs exist
        user_workspace = self._get_user_workspace(user_id)
        user_memory = self._get_user_memory(user_id)
        user_memory_store = self._get_user_memory_store(user_id)

        # Get container if isolation is enabled
        container_id = None
        if self.container_manager and self.container_manager.enabled:
            try:
                container = self.container_manager.get_or_create_container(str(user_id))
                if container:
                    container_id = container.id
                    logger.info(f"🐳 Using container {container.name} ({container_id[:12]}) for user_{user_id}")
            except Exception as e:
                logger.error(f"❌ Failed to get container for user_{user_id}: {e}")
                # Fallback to local execution? Or fail?
                # For now, log error and proceed locally if container fails, but ideally should fail if strict isolation required.
                pass

        try:
            from core.config import Config

            # Загружаем config.yaml, но жёстко переопределяем working_directory на user workspace.
            cfg = Config(config_path="config.yaml", working_directory=str(user_workspace))
            factory = AgentFactory(
                config=cfg,
                working_directory=str(user_workspace),
                broadcaster=None,  # избегаем flood control
                unified_memory=user_memory,  # per-user контекст
                memory_store=user_memory_store,  # per-user SQLite память
                container_id=container_id,  # Pass container ID
            )
            self.user_agent_factories[user_id] = factory
            logger.info(f"✅ Created per-user AgentFactory for user_{user_id}: cwd={user_workspace}")
            return factory
        except Exception as e:
            logger.error(f"❌ Failed to create per-user AgentFactory for user_{user_id}: {e}")
            logger.error(traceback.format_exc())
            return None

    def _get_user_workspace(self, user_id: int) -> Path:
        """
        Получить workspace пользователя для агентов, создать если не существует.

        Возвращает: workspace/user_{user_id} - корневая директория пользователя
        """
        if user_id not in self.user_workspaces:
            # Корневая директория пользователя: workspace/user_{user_id}
            user_workspace = self.config.workspace_path / f"user_{user_id}"
            user_workspace.mkdir(parents=True, exist_ok=True)

            self.user_workspaces[user_id] = user_workspace
            logger.info(f"✅ Created workspace for user_{user_id}: {user_workspace}")
        else:
            logger.debug(f"Using cached workspace for user_{user_id}: {self.user_workspaces[user_id]}")

        return self.user_workspaces[user_id]

    def _get_user_memory(self, user_id: int) -> UnifiedMemory:
        """
        Получить контекст разговора пользователя (UnifiedMemory = ContextManager),
        создать если не существует. Долгосрочная память — в SQLite (MemoryStore).
        """
        if user_id not in self.user_memories:
            user_base_dir = self.config.workspace_path / f"user_{user_id}"
            user_base_dir.mkdir(parents=True, exist_ok=True)

            user_persist = self.config.persist_path / f"user_{user_id}"
            user_persist.mkdir(parents=True, exist_ok=True)

            user_memory = UnifiedMemory(
                workspace=user_base_dir,
                persist_path=user_persist,
                max_history=self.config.max_message_history
            )
            self.user_memories[user_id] = user_memory

            logger.info(f"Создана память для user_{user_id}: workspace={user_base_dir}, persist={user_persist}")

        return self.user_memories[user_id]

    def _get_user_memory_store(self, user_id: int) -> MemoryStore:
        """
        Получить SQLite-based MemoryStore пользователя, создать если не существует.

        Новая система памяти на основе SQLite, заменяет файловую UnifiedMemory.
        """
        if user_id not in self.user_memory_stores:
            # Базовая директория пользователя: workspace/user_{user_id}
            user_base_dir = self.config.workspace_path / f"user_{user_id}"
            user_base_dir.mkdir(parents=True, exist_ok=True)

            # SQLite база данных: workspace/user_{user_id}/memory.db
            db_path = user_base_dir / "memory.db"

            # Создать MemoryStore для пользователя
            user_memory_store = MemoryStore(db_path=str(db_path))
            self.user_memory_stores[user_id] = user_memory_store

            logger.info(f"✅ Создан MemoryStore для user_{user_id}: {db_path}")

        return self.user_memory_stores[user_id]

    def _get_speech_processor(self):
        """
        Возвращает SpeechProcessor (lazy init).
        Читает настройки из voice: секции config.yaml.
        Возвращает None если voice.enabled=false или зависимости недоступны.
        """
        if self._speech_processor is not None:
            return self._speech_processor

        try:
            import yaml
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg_data = yaml.safe_load(f)
            voice_cfg = cfg_data.get("voice", {})
            if not voice_cfg.get("enabled", False):
                return None
            from core.speech_processor import get_speech_processor
            self._speech_processor = get_speech_processor(voice_cfg)
            return self._speech_processor
        except Exception as e:
            logger.error(f"❌ SpeechProcessor инициализация провалилась: {e}")
            logger.error("   Проверь зависимости: pip install faster-whisper pydub scipy")
            logger.error("   Или отключи: voice.enabled: false в config.yaml")
            return None

    async def _warmup_speech_processor(self) -> None:
        """
        Запускает предзагрузку STT/TTS моделей в фоне при старте бота.
        Вызывается как asyncio.Task из initialize() — не блокирует старт.
        После прогрева первый реальный голосовой запрос не тратит время на загрузку.
        """
        sp = self._get_speech_processor()
        if sp is None:
            return  # voice.enabled=false или зависимости недоступны
        try:
            await sp.warmup()
        except Exception as e:
            logger.warning(f"⚠ Прогрев голосовых моделей завершился с ошибкой: {e}")

    async def initialize(self):
        """Инициализация всех компонентов (Layer 1: Connection resilience)"""
        try:
            logger.info("Запуск инициализации TelegramBridge...")

            # 1. Telegram Application (увеличенные таймауты для медленного TLS)
            request_kw: Dict[str, Any] = dict(
                connect_timeout=60.0,
                read_timeout=60.0,
                write_timeout=60.0,
                pool_timeout=60.0,
            )
            if self.config.proxy_url:
                request_kw["proxy"] = self.config.proxy_url
                logger.info(f"Используется прокси: {self.config.proxy_url}")
            request = HTTPXRequest(**request_kw)
            # Timeouts are set via request/get_updates_request; builder timeout params
            # cannot be used when a custom request instance is set.
            self.app = (
                Application.builder()
                .token(self.config.telegram_token)
                .request(request)
                .get_updates_request(HTTPXRequest(**request_kw))
                .build()
            )
            logger.info("✅ Telegram Application создан")

            # 2. SkillsIntegration (опционально)
            if SKILLS_AVAILABLE:
                try:
                    skills_loader = SkillsLoader(workspace=self.config.workspace_path)
                    self.skills = SkillsIntegration(skills_loader=skills_loader)
                    result = self.skills.register_all_skills()
                    logger.info(f"✅ SkillsIntegration: {result['success']} навыков зарегистрировано, {result['failure']} ошибок")
                except Exception as e:
                    logger.warning(f"⚠️ SkillsIntegration не доступен: {e}")
                    self.skills = None

            # 3. AgentFactory (главная система)
            # ВАЖНО: НЕ создаём глобальную AgentFactory.
            # Для TG используем строго per-user фабрики (см. _get_user_agent_factory),
            # чтобы рабочая директория никогда не была cwd проекта/конфига.
            self.agent_factory = None

            # 4. Регистрация обработчиков
            self._register_handlers()
            logger.info("✅ Обработчики команд зарегистрированы")

            # 5. Установка команд бота
            await self._setup_bot_commands()

            # 6. Предзагрузка голосовых моделей (фоново, не блокируя старт)
            asyncio.create_task(self._warmup_speech_processor())

            logger.info("🚀 TelegramBridge полностью инициализирован")

        except Exception as e:
            logger.error(f"❌ Критическая ошибка при инициализации: {e}")
            logger.error(traceback.format_exc())
            raise

    def _register_handlers(self):
        """Регистрация обработчиков команд и сообщений"""
        if not self.app:
            return

        # Команды бота
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("clear", self.cmd_clear))
        self.app.add_handler(CommandHandler("memory", self.cmd_memory))
        self.app.add_handler(CommandHandler("skills", self.cmd_skills))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("sendfile", self.cmd_sendfile))

        # Обработчик текстовых сообщений
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # Обработчики файлов
        self.app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        self.app.add_handler(MessageHandler(filters.AUDIO, self.handle_audio))
        self.app.add_handler(MessageHandler(filters.VIDEO, self.handle_video))
        self.app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))

        # Обработчик ошибок
        self.app.add_error_handler(self.error_handler)

    async def _setup_bot_commands(self):
        """Установка команд в меню Telegram"""
        if not self.app or not self.app.bot:
            return

        commands = [
            BotCommand("start", "Запустить бота"),
            BotCommand("help", "Показать справку"),
            BotCommand("clear", "Очистить историю диалога"),
            BotCommand("memory", "Показать содержимое памяти"),
            BotCommand("skills", "Показать доступные навыки"),
            BotCommand("status", "Показать статус системы"),
            BotCommand("sendfile", "Отправить файл"),
        ]

        try:
            await self.app.bot.set_my_commands(commands)
            logger.info("Команды бота установлены")
        except Exception as e:
            logger.warning(f"Не удалось установить команды бота (возможно проблемы с сетью): {e}")
            # Не прерываем инициализацию, бот может работать без команд в меню

    # ===== ОБРАБОТЧИКИ КОМАНД (Layer 2: Message processing resilience) =====

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /start"""
        try:
            user_id = update.effective_user.id

            # Проверка доступа
            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            welcome_text = (
                "🤖 <b>Unified Agent Bot</b>\n\n"
                "Я объединяю систему агентов grid с памятью и навыками nanobot.\n\n"
                "<b>Доступные команды:</b>\n"
                "/help - справка\n"
                "/clear - очистить историю диалога\n"
                "/memory - показать память\n"
                "/skills - показать навыки\n"
                "/status - статус системы\n"
                "/sendfile - отправить файл\n\n"
                "<b>Работа с файлами:</b>\n"
                "📤 Отправьте мне файл - я сохраню его в вашу рабочую директорию\n"
                "📥 Используйте /sendfile для получения файлов\n\n"
                "Просто отправьте мне сообщение, и я запущу агента для его обработки!"
            )

            await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_start: {e}")
            await self._send_error_message(update, "Ошибка при обработке команды /start")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /help"""
        try:
            help_text = (
                "📚 <b>Справка</b>\n\n"
                "<b>Как работает бот:</b>\n"
                "1. Вы отправляете сообщение\n"
                "2. Бот запускает агента для обработки\n"
                "3. Все действия агента показываются в реальном времени\n"
                "4. Детали скрыты под спойлерами (нажмите чтобы раскрыть)\n\n"
                "<b>Команды:</b>\n"
                "• /clear - очистить краткосрочную память (историю диалога)\n"
                "• /memory - показать долгосрочную и краткосрочную память\n"
                "• /skills - список доступных навыков агента\n"
                "• /status - статистика работы системы\n"
                "• /sendfile - отправить файл из workspace\n\n"
                "<b>Работа с файлами:</b>\n"
                "📤 <b>Отправка файлов боту:</b>\n"
                "• Документы (.pdf, .txt, .doc, и др.)\n"
                "• Изображения (фото)\n"
                "• Аудио и голосовые сообщения\n"
                "• Видео\n"
                "• Максимальный размер: 20MB (документы), 50MB (видео)\n\n"
                "📥 <b>Получение файлов от бота:</b>\n"
                "• <code>/sendfile путь/к/файлу</code>\n"
                "• Путь относительно вашего workspace\n"
                "• Пример: <code>/sendfile telegram_files/document.pdf</code>\n\n"
                "<b>Особенности:</b>\n"
                "✨ Полная прозрачность - видны все действия агентов и подагентов\n"
                "💾 Гибридная память - краткосрочная + долгосрочная\n"
                "🎯 Навыки - агент знает специализированные инструкции\n"
                "📁 Работа с файлами - отправка и получение любых типов файлов\n"
                "🛡️ Устойчивость к ошибкам - 5 уровней защиты"
            )

            await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_help: {e}")
            await self._send_error_message(update, "Ошибка при обработке команды /help")

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /clear - очистить историю диалога"""
        try:
            chat_id = update.effective_chat.id
            user_id = update.effective_user.id

            # Очистить контекст чата
            if chat_id in self.chat_contexts:
                self.chat_contexts[chat_id] = []
                logger.info(f"Контекст чата {chat_id} очищен")

            # Очистить память пользователя
            user_memory = self._get_user_memory(user_id)
            user_memory.context_manager.clear_history()
            logger.info(f"Память user_{user_id} очищена")

            await update.message.reply_text(
                "✅ <b>История диалога очищена</b>\n\n"
                "Контекст чата сброшен.\n"
                "Память в SQLite не затронута.",
                parse_mode=ParseMode.HTML
            )

        except Exception as e:
            logger.error(f"Ошибка в cmd_clear: {e}")
            await self._send_error_message(update, "Ошибка при очистке памяти")

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /memory - показать содержимое памяти (SQLite)"""
        try:
            user_id = update.effective_user.id

            # Получить SQLite MemoryStore пользователя
            memory_store = self._get_user_memory_store(user_id)

            # Получить статистику
            stats = memory_store.get_stats()

            if stats["total_entries"] == 0:
                await update.message.reply_text("📭 Память пуста")
                return

            # Формируем вывод памяти
            output_parts = []

            # Статистика
            header = (
                "💾 <b>Содержимое памяти (SQLite)</b>\n\n"
                f"📊 <b>Статистика:</b>\n"
                f"  • Всего записей: {stats['total_entries']}\n"
                f"  • Архивных: {stats['archived_entries']}\n"
            )

            # По типам
            if stats['by_type']:
                header += "  • По типам:\n"
                for mem_type, count in stats['by_type'].items():
                    header += f"    - {mem_type}: {count}\n"

            # По статусам (задачи)
            if stats.get('by_status'):
                header += "  • Задачи по статусам:\n"
                for status, count in stats['by_status'].items():
                    header += f"    - {status}: {count}\n"

            header += f"\n📁 База данных: {stats['db_size_mb']:.2f} MB\n\n"
            output_parts.append(header)

            # Долгосрочная память
            long_term = memory_store.search(type="long_term", limit=50)
            if long_term:
                lt_text = "🧠 <b>ДОЛГОСРОЧНАЯ ПАМЯТЬ</b> (важная информация):\n\n"
                for entry in long_term:
                    date = entry.created_at[:10] if entry.created_at else "N/A"
                    importance_stars = "⭐" * int(entry.importance * 3)
                    tags_str = f" [{entry.tags}]" if entry.tags else ""
                    lt_text += f"• {date} {importance_stars}{tags_str}\n  {entry.content}\n\n"
                output_parts.append(lt_text)

            # Краткосрочная память
            short_term = memory_store.search(type="short_term", limit=20)
            if short_term:
                st_text = "📝 <b>КРАТКОСРОЧНАЯ ПАМЯТЬ</b> (недавние заметки):\n\n"
                for entry in short_term:
                    date = entry.created_at[:10] if entry.created_at else "N/A"
                    tags_str = f" [{entry.tags}]" if entry.tags else ""
                    st_text += f"• {date}{tags_str}\n  {entry.content}\n\n"
                output_parts.append(st_text)

            # Активные задачи
            active_tasks = memory_store.search(type="task", status="active", limit=10)
            if active_tasks:
                task_text = "📋 <b>АКТИВНЫЕ ЗАДАЧИ</b>:\n\n"
                for task in active_tasks:
                    date = task.created_at[:10] if task.created_at else "N/A"
                    task_text += f"• {date} - {task.content}\n"

                    # Найти план задачи
                    if task.task_id:
                        plans = memory_store.search(type="task_plan", task_id=task.task_id, limit=1)
                        if plans:
                            task_text += f"  └─ План: {plans[0].content}\n"
                    task_text += "\n"
                output_parts.append(task_text)

            # Завершенные задачи (последние 5)
            completed_tasks = memory_store.search(type="task", status="completed", limit=5)
            if completed_tasks:
                ct_text = "✅ <b>ЗАВЕРШЕННЫЕ ЗАДАЧИ</b> (последние 5):\n\n"
                for task in completed_tasks:
                    date = task.created_at[:10] if task.created_at else "N/A"
                    ct_text += f"• {date} - {task.content}\n"
                output_parts.append(ct_text)

            # Объединить все части и отправить
            full_output = "".join(output_parts)

            # Разбить на части если слишком длинное (Telegram limit ~4096)
            max_length = 4000
            if len(full_output) <= max_length:
                await update.message.reply_text(full_output, parse_mode=ParseMode.HTML)
            else:
                # Отправить по частям
                parts = [full_output[i:i+max_length] for i in range(0, len(full_output), max_length)]
                await update.message.reply_text(
                    f"💾 <b>Содержимое памяти ({len(parts)} частей):</b>",
                    parse_mode=ParseMode.HTML
                )
                for i, part in enumerate(parts, 1):
                    message = f"<b>Часть {i}/{len(parts)}:</b>\n\n{part}"
                    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_memory: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при получении памяти")

    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /skills - показать доступные навыки"""
        try:
            if not self.skills:
                await update.message.reply_text("⚠️ Система навыков недоступна")
                return

            # Получить список навыков
            skills_list = self.skills.loader.list_skills(filter_unavailable=True)

            if not skills_list:
                await update.message.reply_text("📭 Нет доступных навыков")
                return

            # Форматировать список
            skills_text = "🎯 <b>Доступные навыки:</b>\n\n"

            for skill_info in skills_list:
                name = skill_info.get('name', 'unknown')
                description = skill_info.get('description', 'Нет описания')
                always_loaded = skill_info.get('always', False)

                icon = "⭐" if always_loaded else "📄"
                skills_text += f"{icon} <b>{name}</b>\n"
                skills_text += f"   {description}\n\n"

            skills_text += f"\n<i>Всего навыков: {len(skills_list)}</i>\n"
            skills_text += "<i>⭐ = всегда загружен в инструкции агента</i>"

            await update.message.reply_text(skills_text, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_skills: {e}")
            await self._send_error_message(update, "Ошибка при получении навыков")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /status - показать статус системы"""
        try:
            uptime = datetime.now() - self.stats["start_time"]

            status_text = (
                "📊 <b>Статус системы</b>\n\n"
                f"⏱️ <b>Uptime:</b> {uptime}\n"
                f"📨 <b>Сообщений обработано:</b> {self.stats['messages_processed']}\n"
                f"🤖 <b>Агентов запущено:</b> {self.stats['agents_launched']}\n"
                f"⚠️ <b>Ошибок обработано:</b> {self.stats['errors_handled']}\n"
                f"🔄 <b>Активных задач:</b> {len(self.active_tasks)}\n\n"
                "<b>Компоненты:</b>\n"
                f"{'✅' if self.broadcaster else '❌'} LiveTransparencyBroadcaster\n"
                f"{'✅' if True else '❌'} UnifiedMemory (per-user)\n"
                f"{'✅' if self.skills else '❌'} SkillsIntegration\n"
                f"{'✅' if AGENT_FACTORY_AVAILABLE else '❌'} AgentFactory (per-user)\n"
            )

            await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_status: {e}")
            await self._send_error_message(update, "Ошибка при получении статуса")

    # ===== ОБРАБОТКА СООБЩЕНИЙ (Layer 2 + 3: Message and Agent resilience) =====

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка входящего текстового сообщения"""
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id
            message_text = update.message.text

            # Проверка доступа
            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Проверка на активную задачу
            if chat_id in self.active_tasks and not self.active_tasks[chat_id].done():
                await update.message.reply_text(
                    "⏳ Предыдущий запрос еще обрабатывается. Подождите завершения."
                )
                return

            logger.info(f"Получено сообщение от {user_id}: {message_text[:100]}")
            self.stats["messages_processed"] += 1

            # Инициализировать контекст чата если его нет
            if chat_id not in self.chat_contexts:
                self.chat_contexts[chat_id] = []

            # Добавить сообщение пользователя в контекст чата
            self.chat_contexts[chat_id].append({
                "role": "user",
                "content": message_text
            })

            # Ограничить размер контекста
            max_context = self.config.max_message_history * 2  # user + assistant пары
            if len(self.chat_contexts[chat_id]) > max_context:
                self.chat_contexts[chat_id] = self.chat_contexts[chat_id][-max_context:]

            # Сохранить в память пользователя
            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("user", message_text)

            # Запустить обработку в отдельной задаче
            task = asyncio.create_task(
                self._process_user_message(chat_id, user_id, message_text)
            )
            self.active_tasks[chat_id] = task

            # Очистить завершенную задачу
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка в handle_message: {e}")
            logger.error(traceback.format_exc())
            self.stats["errors_handled"] += 1
            await self._send_error_message(update, "Ошибка при обработке сообщения")

    async def _process_user_message(self, chat_id: int, user_id: int, message_text: str):
        """Обработка сообщения пользователя с запуском агента (Layer 3: Agent resilience)"""
        try:
            # Получить память пользователя (deprecated - для обратной совместимости)
            user_memory = self._get_user_memory(user_id)

            # Получить SQLite MemoryStore пользователя (new)
            user_memory_store = self._get_user_memory_store(user_id)

            # Получить рабочую директорию пользователя
            user_workspace = self._get_user_workspace(user_id)

            # Установить голосовой контекст для инструмента send_voice_reply
            try:
                from core.voice_context import set_voice_context
                set_voice_context(self.app.bot, chat_id, str(user_workspace))
            except Exception as e:
                logger.debug(f"Не удалось установить voice_context: {e}")

            # Отправить одно простое сообщение о начале работы
            status_message = await self.app.bot.send_message(
                chat_id=chat_id,
                text="🤖 Обрабатываю запрос..."
            )

            # Запустить агента
            user_factory = self._get_user_agent_factory(user_id)
            if not user_factory:
                response = "⚠️ AgentFactory недоступен. Система работает в ограниченном режиме."
            else:
                self.stats["agents_launched"] += 1

                # Sanity log: cwd must always be user workspace for TG.
                try:
                    wd = user_factory.config.get_working_directory()
                    if Path(wd).resolve() != user_workspace.resolve():
                        logger.warning(
                            "⚠️ TG invariant violated: AgentFactory working_directory mismatch "
                            f"(expected={user_workspace}, actual={wd})"
                        )
                except Exception:
                    pass

                # Запуск агента через per-user AgentFactory (без переключений cwd)
                try:
                    default_agent_key = user_factory.config.get_default_agent()

                    chat_history = self.chat_contexts.get(chat_id, [])
                    if len(chat_history) > 1:
                        context_text = "\n".join([
                            f"{'Пользователь' if msg['role'] == 'user' else 'Ассистент'}: {msg['content']}"
                            for msg in chat_history[:-1]
                        ])
                        message_with_context = f"[Контекст диалога]\n{context_text}\n\n[Текущий вопрос]\n{message_text}"
                    else:
                        message_with_context = message_text

                    response = await user_factory.run_agent(
                        agent_key=default_agent_key,
                        message=message_with_context,
                        use_active_context=False,
                        user_id=str(user_id),
                        stream=True,
                    )
                except Exception as agent_error:
                    logger.error(f"Ошибка запуска агента: {agent_error}")
                    logger.error(traceback.format_exc())
                    response = f"❌ Ошибка при выполнении запроса:\n\n{str(agent_error)}"

            # Сохранить ответ в контекст чата
            if chat_id in self.chat_contexts:
                self.chat_contexts[chat_id].append({
                    "role": "assistant",
                    "content": response
                })

            # Сохранить ответ в память пользователя
            user_memory.add_message("assistant", response)

            # Удалить сообщение о статусе
            try:
                await self.app.bot.delete_message(chat_id=chat_id, message_id=status_message.message_id)
            except Exception as e:
                logger.debug(f"Не удалось удалить статусное сообщение: {e}")

            # Отправить финальный ответ (несколькими сообщениями, если текст длинный)
            escaped_response = self._escape_html(response)
            await self._send_long_message(
                chat_id=chat_id,
                text=escaped_response,
                parse_mode=ParseMode.HTML,
                first_prefix="✅ <b>Ответ:</b>\n\n",
            )

        except Exception as e:
            logger.error(f"Ошибка в _process_user_message: {e}")
            logger.error(traceback.format_exc())
            self.stats["errors_handled"] += 1

            # Отправить сообщение об ошибке (несколькими сообщениями, если длинное)
            error_body = f"<code>{self._escape_html(str(e))}</code>"
            await self._send_long_message(
                chat_id=chat_id,
                text=error_body,
                parse_mode=ParseMode.HTML,
                first_prefix="❌ <b>Ошибка при обработке запроса:</b>\n\n",
            )

    # ===== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ =====

    def _escape_html(self, text: str) -> str:
        """Экранирование HTML для безопасной отправки в Telegram"""
        return html.escape(text)

    async def _send_long_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = ParseMode.HTML,
        first_prefix: str = "",
    ) -> None:
        """Отправляет длинный текст несколькими сообщениями, не превышая лимит Telegram."""
        if not text and not first_prefix:
            return
        if first_prefix and not text:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=first_prefix,
                parse_mode=parse_mode,
            )
            return
        max_len = TELEGRAM_MAX_MESSAGE_LENGTH
        first_max = max(1, max_len - len(first_prefix)) if first_prefix else max_len

        offset = 0
        is_first = True
        while offset < len(text):
            chunk_size = first_max if (is_first and first_prefix) else max_len
            chunk = text[offset : offset + chunk_size]
            offset += len(chunk)
            if is_first and first_prefix:
                chunk = first_prefix + chunk
                is_first = False
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
            )
            if offset < len(text):
                await asyncio.sleep(0.25)

    def _check_access(self, user_id: int) -> bool:
        """Проверка доступа пользователя"""
        if self.config.allowed_users is None:
            return True  # Все пользователи разрешены
        return user_id in self.config.allowed_users

    async def _send_error_message(self, update: Update, error_text: str):
        """Отправка сообщения об ошибке"""
        try:
            await update.message.reply_text(f"❌ {error_text}")
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Глобальный обработчик ошибок (Layer 1: Connection resilience)"""
        logger.error(f"Необработанная ошибка: {context.error}")
        logger.error(traceback.format_exc())
        self.stats["errors_handled"] += 1

        # Попытаться отправить сообщение пользователю
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="❌ Произошла непредвиденная ошибка. Попробуйте позже."
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение об ошибке: {e}")

    # ===== ОБРАБОТКА ФАЙЛОВ =====

    async def _save_file(self, file_obj, user_id: int, file_type: str) -> tuple[Path, str]:
        """
        Сохранение файла в workspace пользователя

        Returns:
            tuple[Path, str]: (путь к файлу, оригинальное имя файла)
        """
        try:
            # Получить workspace пользователя (workspace/user_{user_id})
            user_workspace = self._get_user_workspace(user_id)

            # Создать директорию для файлов если не существует
            # Файлы будут в workspace/user_{user_id}/telegram_files
            files_dir = user_workspace / "telegram_files"
            files_dir.mkdir(exist_ok=True)

            # Скачать файл
            file_info = await file_obj.get_file()

            # Определить имя файла
            file_name = getattr(file_obj, 'file_name', None)
            if not file_name:
                # Если нет имени, сгенерировать из file_id и типа
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                ext = ""
                if file_type == "photo":
                    ext = ".jpg"
                elif file_type == "audio":
                    ext = ".mp3"
                elif file_type == "video":
                    ext = ".mp4"
                elif file_type == "voice":
                    ext = ".ogg"
                file_name = f"{file_type}_{timestamp}{ext}"

            # Путь для сохранения
            file_path = files_dir / file_name

            # Скачать файл
            await file_info.download_to_drive(custom_path=str(file_path))

            logger.info(f"Файл сохранен: {file_path}")
            return file_path, file_name

        except Exception as e:
            logger.error(f"Ошибка при сохранении файла: {e}")
            raise

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка документов (файлов)"""
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id

            # Проверка доступа
            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Проверка на активную задачу
            if chat_id in self.active_tasks and not self.active_tasks[chat_id].done():
                await update.message.reply_text(
                    "⏳ Предыдущий запрос еще обрабатывается. Подождите завершения."
                )
                return

            document = update.message.document
            file_name = document.file_name
            file_size = document.file_size

            # Получить caption (текст сообщения с файлом)
            caption = update.message.caption or ""

            logger.info(f"Получен документ от {user_id}: {file_name} ({file_size} bytes), caption: {caption[:50] if caption else 'нет'}")

            # Проверка размера файла (ограничение 20MB)
            max_size = 20 * 1024 * 1024  # 20MB
            if file_size > max_size:
                await update.message.reply_text(
                    f"❌ Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). "
                    f"Максимальный размер: {max_size / 1024 / 1024:.0f} MB"
                )
                return

            # Сохранить файл
            status_msg = None
            try:
                status_msg = await update.message.reply_text("📥 Скачиваю файл...")
            except Exception as e:
                logger.warning(f"Не удалось отправить статусное сообщение (timeout): {e}")

            file_path, original_name = await self._save_file(document, user_id, "document")

            # Обновить статус или отправить новое сообщение
            try:
                if status_msg:
                    await status_msg.edit_text(
                        f"✅ Файл получен и сохранен:\n\n"
                        f"📄 <b>Имя:</b> <code>{original_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                        f"🤖 Передаю агенту для обработки...",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_text(
                        f"✅ Файл получен и сохранен:\n\n"
                        f"📄 <b>Имя:</b> <code>{original_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                        f"🤖 Передаю агенту для обработки...",
                        parse_mode=ParseMode.HTML
                    )
            except Exception as e:
                logger.warning(f"Не удалось обновить статус (timeout): {e}")

            # Добавить информацию о файле в память пользователя (deprecated - для обратной совместимости)
            user_memory = self._get_user_memory(user_id)
            user_memory.add_message(
                "system",
                f"Пользователь отправил файл: {original_name} (путь: {file_path})"
            )

            # Инициализировать контекст чата если его нет
            if chat_id not in self.chat_contexts:
                self.chat_contexts[chat_id] = []

            # Сформировать сообщение для агента с информацией о файле
            # Передаем абсолютный путь к файлу для инструментов
            file_info_message = (
                f"Пользователь отправил файл:\n"
                f"Имя: {original_name}\n"
                f"Путь: {file_path}\n"
                f"Размер: {file_size / 1024:.2f} KB\n"
            )

            # Добавить текст сообщения (caption) если есть
            if caption:
                file_info_message += f"\n📝 Сообщение пользователя: {caption}\n\n"
                file_info_message += "Обработай запрос пользователя, используя прикрепленный файл."
            else:
                file_info_message += "\nОпредели тип файла и предложи что можно с ним сделать."

            # Добавить сообщение о файле в контекст чата
            self.chat_contexts[chat_id].append({
                "role": "user",
                "content": file_info_message
            })

            # Ограничить размер контекста
            max_context = self.config.max_message_history * 2
            if len(self.chat_contexts[chat_id]) > max_context:
                self.chat_contexts[chat_id] = self.chat_contexts[chat_id][-max_context:]

            # Запустить обработку агентом в отдельной задаче
            task = asyncio.create_task(
                self._process_user_message(chat_id, user_id, file_info_message)
            )
            self.active_tasks[chat_id] = task

            # Очистить завершенную задачу
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка при обработке документа: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при обработке файла")

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка фотографий"""
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id

            # Проверка доступа
            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Проверка на активную задачу
            if chat_id in self.active_tasks and not self.active_tasks[chat_id].done():
                await update.message.reply_text(
                    "⏳ Предыдущий запрос еще обрабатывается. Подождите завершения."
                )
                return

            # Получить самое большое фото из массива
            photo = update.message.photo[-1]
            file_size = photo.file_size

            # Получить caption (текст сообщения с фото)
            caption = update.message.caption or ""

            logger.info(f"Получено фото от {user_id} ({file_size} bytes), caption: {caption[:50] if caption else 'нет'}")

            # Сохранить файл
            status_msg = None
            try:
                status_msg = await update.message.reply_text("📥 Скачиваю фото...")
            except Exception as e:
                logger.warning(f"Не удалось отправить статусное сообщение (timeout): {e}")

            file_path, file_name = await self._save_file(photo, user_id, "photo")

            # Обновить статус или отправить новое сообщение
            try:
                if status_msg:
                    await status_msg.edit_text(
                        f"✅ Фото получено и сохранено:\n\n"
                        f"🖼️ <b>Файл:</b> <code>{file_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                        f"🤖 Передаю агенту для анализа...",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_text(
                        f"✅ Фото получено и сохранено:\n\n"
                        f"🖼️ <b>Файл:</b> <code>{file_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                        f"🤖 Передаю агенту для анализа...",
                        parse_mode=ParseMode.HTML
                    )
            except Exception as e:
                logger.warning(f"Не удалось обновить статус (timeout): {e}")

            # Добавить в память (deprecated - для обратной совместимости)
            user_memory = self._get_user_memory(user_id)
            user_memory.add_message(
                "system",
                f"Пользователь отправил фото (путь: {file_path})"
            )

            # Инициализировать контекст чата если его нет
            if chat_id not in self.chat_contexts:
                self.chat_contexts[chat_id] = []

            # Сформировать сообщение для агента с информацией о фото
            # Передаем абсолютный путь к файлу для инструментов
            file_info_message = (
                f"Пользователь отправил фото:\n"
                f"Имя файла: {file_name}\n"
                f"Путь: {file_path}\n"
                f"Размер: {file_size / 1024:.2f} KB\n"
            )

            # Добавить текст сообщения (caption) если есть
            if caption:
                file_info_message += f"\n📝 Сообщение пользователя: {caption}\n\n"
                file_info_message += "Обработай запрос пользователя, используя прикрепленное изображение."
            else:
                file_info_message += "\nПроанализируй изображение и опиши что на нём изображено."

            # Добавить сообщение о файле в контекст чата
            self.chat_contexts[chat_id].append({
                "role": "user",
                "content": file_info_message
            })

            # Ограничить размер контекста
            max_context = self.config.max_message_history * 2
            if len(self.chat_contexts[chat_id]) > max_context:
                self.chat_contexts[chat_id] = self.chat_contexts[chat_id][-max_context:]

            # Запустить обработку агентом в отдельной задаче
            task = asyncio.create_task(
                self._process_user_message(chat_id, user_id, file_info_message)
            )
            self.active_tasks[chat_id] = task

            # Очистить завершенную задачу
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка при обработке фото: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при обработке фото")

    async def handle_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка аудио файлов"""
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Проверка на активную задачу
            if chat_id in self.active_tasks and not self.active_tasks[chat_id].done():
                await update.message.reply_text(
                    "⏳ Предыдущий запрос еще обрабатывается. Подождите завершения."
                )
                return

            audio = update.message.audio
            file_name = audio.file_name or "audio.mp3"
            file_size = audio.file_size

            # Получить caption (текст сообщения с аудио)
            caption = update.message.caption or ""

            logger.info(f"Получено аудио от {user_id}: {file_name}, caption: {caption[:50] if caption else 'нет'}")

            status_msg = None
            try:
                status_msg = await update.message.reply_text("📥 Скачиваю аудио...")
            except Exception as e:
                logger.warning(f"Не удалось отправить статусное сообщение (timeout): {e}")

            file_path, original_name = await self._save_file(audio, user_id, "audio")

            try:
                if status_msg:
                    await status_msg.edit_text(
                        f"✅ Аудио получено и сохранено:\n\n"
                        f"🎵 <b>Файл:</b> <code>{original_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                        f"🤖 Передаю агенту для обработки...",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_text(
                        f"✅ Аудио получено и сохранено:\n\n"
                        f"🎵 <b>Файл:</b> <code>{original_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                        f"🤖 Передаю агенту для обработки...",
                        parse_mode=ParseMode.HTML
                    )
            except Exception as e:
                logger.warning(f"Не удалось обновить статус (timeout): {e}")

            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("system", f"Пользователь отправил аудио: {original_name}")

            # Инициализировать контекст чата если его нет
            if chat_id not in self.chat_contexts:
                self.chat_contexts[chat_id] = []

            # Сформировать сообщение для агента
            file_info_message = (
                f"Пользователь отправил аудио файл:\n"
                f"Имя: {original_name}\n"
                f"Путь: {file_path}\n"
                f"Размер: {file_size / 1024:.2f} KB\n"
            )

            # Добавить текст сообщения (caption) если есть
            if caption:
                file_info_message += f"\n📝 Сообщение пользователя: {caption}\n\n"
                file_info_message += "Обработай запрос пользователя, используя прикрепленный аудио файл."
            else:
                file_info_message += "\nПредложи что можно сделать с этим аудио файлом."

            # Добавить сообщение о файле в контекст чата
            self.chat_contexts[chat_id].append({
                "role": "user",
                "content": file_info_message
            })

            # Ограничить размер контекста
            max_context = self.config.max_message_history * 2
            if len(self.chat_contexts[chat_id]) > max_context:
                self.chat_contexts[chat_id] = self.chat_contexts[chat_id][-max_context:]

            # Запустить обработку агентом
            task = asyncio.create_task(
                self._process_user_message(chat_id, user_id, file_info_message)
            )
            self.active_tasks[chat_id] = task
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка при обработке аудио: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при обработке аудио")

    async def handle_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка видео файлов"""
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Проверка на активную задачу
            if chat_id in self.active_tasks and not self.active_tasks[chat_id].done():
                await update.message.reply_text(
                    "⏳ Предыдущий запрос еще обрабатывается. Подождите завершения."
                )
                return

            video = update.message.video
            file_name = video.file_name or "video.mp4"
            file_size = video.file_size

            # Получить caption (текст сообщения с видео)
            caption = update.message.caption or ""

            logger.info(f"Получено видео от {user_id}: {file_name}, caption: {caption[:50] if caption else 'нет'}")

            # Проверка размера (видео может быть большим)
            max_size = 50 * 1024 * 1024  # 50MB
            if file_size > max_size:
                await update.message.reply_text(
                    f"❌ Видео слишком большое ({file_size / 1024 / 1024:.2f} MB). "
                    f"Максимальный размер: {max_size / 1024 / 1024:.0f} MB"
                )
                return

            status_msg = None
            try:
                status_msg = await update.message.reply_text("📥 Скачиваю видео...")
            except Exception as e:
                logger.warning(f"Не удалось отправить статусное сообщение (timeout): {e}")

            file_path, original_name = await self._save_file(video, user_id, "video")

            try:
                if status_msg:
                    await status_msg.edit_text(
                        f"✅ Видео получено и сохранено:\n\n"
                        f"🎬 <b>Файл:</b> <code>{original_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024 / 1024:.2f} MB\n\n"
                        f"🤖 Передаю агенту для обработки...",
                        parse_mode=ParseMode.HTML
                    )
                else:
                    await update.message.reply_text(
                        f"✅ Видео получено и сохранено:\n\n"
                        f"🎬 <b>Файл:</b> <code>{original_name}</code>\n"
                        f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                        f"📊 <b>Размер:</b> {file_size / 1024 / 1024:.2f} MB\n\n"
                        f"🤖 Передаю агенту для обработки...",
                        parse_mode=ParseMode.HTML
                    )
            except Exception as e:
                logger.warning(f"Не удалось обновить статус (timeout): {e}")

            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("system", f"Пользователь отправил видео: {original_name}")

            # Инициализировать контекст чата если его нет
            if chat_id not in self.chat_contexts:
                self.chat_contexts[chat_id] = []

            # Сформировать сообщение для агента
            file_info_message = (
                f"Пользователь отправил видео файл:\n"
                f"Имя: {original_name}\n"
                f"Путь: {file_path}\n"
                f"Размер: {file_size / 1024 / 1024:.2f} MB\n"
            )

            # Добавить текст сообщения (caption) если есть
            if caption:
                file_info_message += f"\n📝 Сообщение пользователя: {caption}\n\n"
                file_info_message += "Обработай запрос пользователя, используя прикрепленное видео."
            else:
                file_info_message += "\nПредложи что можно сделать с этим видео файлом."

            # Добавить сообщение о файле в контекст чата
            self.chat_contexts[chat_id].append({
                "role": "user",
                "content": file_info_message
            })

            # Ограничить размер контекста
            max_context = self.config.max_message_history * 2
            if len(self.chat_contexts[chat_id]) > max_context:
                self.chat_contexts[chat_id] = self.chat_contexts[chat_id][-max_context:]

            # Запустить обработку агентом
            task = asyncio.create_task(
                self._process_user_message(chat_id, user_id, file_info_message)
            )
            self.active_tasks[chat_id] = task
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка при обработке видео: {e}")
            await self._send_error_message(update, "Ошибка при обработке видео")

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка голосовых сообщений"""
        try:
            user_id = update.effective_user.id
            chat_id = update.effective_chat.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Проверка на активную задачу
            if chat_id in self.active_tasks and not self.active_tasks[chat_id].done():
                await update.message.reply_text(
                    "⏳ Предыдущий запрос еще обрабатывается. Подождите завершения."
                )
                return

            voice = update.message.voice
            file_size = voice.file_size

            # Получить caption (текст сообщения с голосовым)
            caption = update.message.caption or ""

            logger.info(f"Получено голосовое сообщение от {user_id}, caption: {caption[:50] if caption else 'нет'}")

            status_msg = None
            try:
                status_msg = await update.message.reply_text("📥 Скачиваю голосовое сообщение...")
            except Exception as e:
                logger.warning(f"Не удалось отправить статусное сообщение (timeout): {e}")

            file_path, file_name = await self._save_file(voice, user_id, "voice")

            # Попытаться распознать речь (STT)
            sp = self._get_speech_processor()
            transcribed_text = None
            stt_error_msg = None

            if sp is None:
                # STT явно недоступен — сообщить пользователю
                stt_error_msg = (
                    "⚠️ <b>Распознавание речи недоступно</b>\n"
                    "Установите зависимости: <code>pip install faster-whisper</code>\n"
                    "Или отключите в конфиге: <code>voice.enabled: false</code>"
                )
            else:
                try:
                    if status_msg:
                        await status_msg.edit_text("🎙️ Распознаю речь...")
                    transcribed_text = await sp.transcribe(file_path)
                    if not transcribed_text:
                        stt_error_msg = "⚠️ Речь не распознана (пустой результат — возможно тишина или шум)"
                    else:
                        logger.info(f"STT ok user_{user_id}: {transcribed_text[:80]}")
                except Exception as stt_err:
                    logger.error(f"STT failed: {stt_err}")
                    stt_error_msg = f"⚠️ <b>Ошибка распознавания речи:</b> <code>{html.escape(str(stt_err))}</code>"

            # Показать ошибку STT пользователю (если есть)
            if stt_error_msg:
                try:
                    await update.message.reply_text(stt_error_msg, parse_mode=ParseMode.HTML)
                except Exception:
                    pass

            if transcribed_text:
                # Показать расшифровку пользователю
                transcription_preview = (transcribed_text[:200] + "...") if len(transcribed_text) > 200 else transcribed_text
                try:
                    if status_msg:
                        await status_msg.edit_text(
                            f"🎤 <b>Расшифровка:</b> <i>{self._escape_html(transcription_preview)}</i>\n\n"
                            f"🤖 Обрабатываю...",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await update.message.reply_text(
                            f"🎤 <b>Расшифровка:</b> <i>{self._escape_html(transcription_preview)}</i>",
                            parse_mode=ParseMode.HTML
                        )
                except Exception as e:
                    logger.warning(f"Не удалось показать расшифровку: {e}")

                # Сформировать сообщение для агента:
                # Префикс [ГОЛОСОВОЕ СООБЩЕНИЕ] — агент знает формат запроса
                user_message = f"[ГОЛОСОВОЕ СООБЩЕНИЕ]\n{transcribed_text}"
                if caption:
                    user_message += f"\n{caption}"
            else:
                # Fallback: STT недоступен или вернул пустой результат
                try:
                    if status_msg:
                        await status_msg.edit_text(
                            f"✅ Голосовое сообщение получено:\n\n"
                            f"🎤 <b>Файл:</b> <code>{file_name}</code>\n"
                            f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                            f"🤖 Передаю агенту для обработки...",
                            parse_mode=ParseMode.HTML
                        )
                    else:
                        await update.message.reply_text(
                            f"✅ Голосовое сообщение получено:\n\n"
                            f"🎤 <b>Файл:</b> <code>{file_name}</code>\n"
                            f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                            f"🤖 Передаю агенту для обработки...",
                            parse_mode=ParseMode.HTML
                        )
                except Exception as e:
                    logger.warning(f"Не удалось обновить статус (timeout): {e}")

                user_message = f"[ГОЛОСОВОЕ СООБЩЕНИЕ]\nФайл: {file_path}\n"
                if caption:
                    user_message += caption
                else:
                    user_message += "Распознавание речи недоступно. Предложи варианты работы с файлом."

            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("system", "Пользователь отправил голосовое сообщение")

            # Инициализировать контекст чата если его нет
            if chat_id not in self.chat_contexts:
                self.chat_contexts[chat_id] = []

            # Добавить сообщение в контекст чата
            self.chat_contexts[chat_id].append({
                "role": "user",
                "content": user_message
            })

            # Ограничить размер контекста
            max_context = self.config.max_message_history * 2
            if len(self.chat_contexts[chat_id]) > max_context:
                self.chat_contexts[chat_id] = self.chat_contexts[chat_id][-max_context:]

            # Запустить обработку агентом
            task = asyncio.create_task(
                self._process_user_message(chat_id, user_id, user_message)
            )
            self.active_tasks[chat_id] = task
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка при обработке голосового сообщения: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при обработке голосового сообщения")

    async def cmd_sendfile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Команда /sendfile - отправить файл пользователю
        Использование: /sendfile <путь_к_файлу>
        """
        try:
            user_id = update.effective_user.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            # Получить аргументы команды
            args = context.args
            if not args:
                await update.message.reply_text(
                    "❓ <b>Использование команды /sendfile:</b>\n\n"
                    "<code>/sendfile &lt;путь_к_файлу&gt;</code>\n\n"
                    "Пример:\n"
                    "<code>/sendfile workspace/file.txt</code>",
                    parse_mode=ParseMode.HTML
                )
                return

            # Путь к файлу
            file_path_str = " ".join(args)

            # Попробовать относительный путь от workspace пользователя
            user_workspace = self._get_user_workspace(user_id)
            file_path = Path(file_path_str)

            # Если путь не абсолютный, сделать его относительным от workspace
            if not file_path.is_absolute():
                file_path = user_workspace / file_path

            # Проверить существование файла
            if not file_path.exists():
                await update.message.reply_text(
                    f"❌ Файл не найден: <code>{file_path}</code>",
                    parse_mode=ParseMode.HTML
                )
                return

            # Проверить что это файл, а не директория
            if not file_path.is_file():
                await update.message.reply_text(
                    f"❌ Указан путь к директории, а не к файлу: <code>{file_path}</code>",
                    parse_mode=ParseMode.HTML
                )
                return

            # Проверить размер файла
            file_size = file_path.stat().st_size
            max_size = 50 * 1024 * 1024  # 50MB (лимит Telegram API)

            if file_size > max_size:
                await update.message.reply_text(
                    f"❌ Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). "
                    f"Максимальный размер для отправки: {max_size / 1024 / 1024:.0f} MB"
                )
                return

            # Отправить файл
            status_msg = await update.message.reply_text("📤 Отправляю файл...")

            with open(file_path, 'rb') as file:
                await update.message.reply_document(
                    document=file,
                    filename=file_path.name,
                    caption=f"📄 <b>Файл:</b> <code>{file_path.name}</code>\n"
                            f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB",
                    parse_mode=ParseMode.HTML
                )

            await status_msg.delete()
            logger.info(f"Файл отправлен пользователю {user_id}: {file_path}")

        except Exception as e:
            logger.error(f"Ошибка при отправке файла: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при отправке файла")

    async def send_file_to_user(self, chat_id: int, file_path: Path, caption: str = None):
        """
        Отправить файл пользователю программно (для использования агентами)

        Args:
            chat_id: ID чата
            file_path: Путь к файлу
            caption: Описание файла (опционально)
        """
        try:
            if not file_path.exists():
                logger.error(f"Файл не найден: {file_path}")
                return False

            if not file_path.is_file():
                logger.error(f"Путь указывает не на файл: {file_path}")
                return False

            file_size = file_path.stat().st_size
            max_size = 50 * 1024 * 1024  # 50MB

            if file_size > max_size:
                logger.error(f"Файл слишком большой: {file_size} bytes")
                return False

            with open(file_path, 'rb') as file:
                await self.app.bot.send_document(
                    chat_id=chat_id,
                    document=file,
                    filename=file_path.name,
                    caption=caption or f"📄 {file_path.name}",
                    parse_mode=ParseMode.HTML
                )

            logger.info(f"Файл отправлен в чат {chat_id}: {file_path}")
            return True

        except Exception as e:
            logger.error(f"Ошибка при отправке файла: {e}")
            logger.error(traceback.format_exc())
            return False

    # ===== УПРАВЛЕНИЕ ЖИЗНЕННЫМ ЦИКЛОМ =====

    async def start(self):
        """Запуск бота"""
        try:
            logger.info("🚀 Запуск TelegramBridge...")

            await self.initialize()

            logger.info("📡 Запуск Telegram polling...")
            await self.app.initialize()
            await self.app.start()
            await self.app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

            logger.info("✅ TelegramBridge успешно запущен")

        except Exception as e:
            logger.error(f"❌ Ошибка при запуске TelegramBridge: {e}")
            logger.error(traceback.format_exc())
            raise

    async def stop(self):
        """Остановка бота. Активные задачи отменяются по таймауту, запросы не ждут бесконечно."""
        shutdown_timeout = 8.0  # секунд на отмену задач и остановку Telegram
        try:
            logger.info("🛑 Остановка TelegramBridge...")

            # Отменить активные задачи и ждать не дольше таймаута
            if self.active_tasks:
                tasks = list(self.active_tasks.values())
                logger.info(f"Отмена {len(tasks)} активных задач (таймаут {shutdown_timeout}s)...")
                for t in tasks:
                    t.cancel()
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=shutdown_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Часть задач не завершилась за таймаут, продолжаем остановку")

            # Остановить broadcaster (если существует)
            if hasattr(self, 'broadcaster') and self.broadcaster:
                self.broadcaster.stop()  # Not async, no await needed

            # Остановить Telegram с таймаутом
            if self.app:
                try:
                    await asyncio.wait_for(self.app.updater.stop(), timeout=shutdown_timeout)
                except asyncio.TimeoutError:
                    logger.warning("updater.stop() по таймауту")
                try:
                    await asyncio.wait_for(self.app.stop(), timeout=shutdown_timeout)
                except asyncio.TimeoutError:
                    logger.warning("app.stop() по таймауту")
                try:
                    await asyncio.wait_for(self.app.shutdown(), timeout=shutdown_timeout)
                except asyncio.TimeoutError:
                    logger.warning("app.shutdown() по таймауту")

            logger.info("✅ TelegramBridge остановлен")

        except Exception as e:
            logger.error(f"Ошибка при остановке TelegramBridge: {e}")
            logger.error(traceback.format_exc())
