"""
TelegramBridge - интеграция Telegram бота с OpenAI Agents SDK
Объединяет nanobot (Telegram, память, скилы) и grid (агенты, оркестрация)
"""

import asyncio
import logging
import traceback
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

from channels.live_transparency import LiveTransparencyBroadcaster, ProgressEvent
from core.unified_memory import UnifiedMemory

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


logger = logging.getLogger(__name__)


@dataclass
class BridgeConfig:
    """Конфигурация TelegramBridge"""
    telegram_token: str
    workspace_path: Path
    persist_path: Path
    max_message_history: int = 15
    enable_skills: bool = True
    enable_transparency: bool = True
    allowed_users: Optional[list] = None  # None = все пользователи


class TelegramBridge:
    """
    Главный мост между Telegram и агентами

    Архитектура:
    - Telegram bot (python-telegram-bot) -> TelegramBridge
    - TelegramBridge -> AgentFactory -> OpenAI Agents
    - LiveTransparencyBroadcaster -> Telegram (обновления в реальном времени)
    - UnifiedMemory (grid ContextManager + nanobot MemoryStore)
    - SkillsIntegration (nanobot skills как grid tools)
    """

    def __init__(self, config: BridgeConfig):
        self.config = config

        # Telegram Application
        self.app: Optional[Application] = None

        # Основные компоненты
        self.broadcaster: Optional[LiveTransparencyBroadcaster] = None
        self.memory: Optional[UnifiedMemory] = None
        self.skills: Optional[SkillsIntegration] = None
        self.agent_factory: Optional[AgentFactory] = None

        # Состояние активных операций
        self.active_tasks: Dict[int, asyncio.Task] = {}  # chat_id -> task

        # Статистика (для команды /status)
        self.stats = {
            "start_time": datetime.now(),
            "messages_processed": 0,
            "agents_launched": 0,
            "errors_handled": 0,
        }

        logger.info("TelegramBridge инициализирован")

    async def initialize(self):
        """Инициализация всех компонентов (Layer 1: Connection resilience)"""
        try:
            logger.info("Запуск инициализации TelegramBridge...")

            # 1. Telegram Application
            self.app = Application.builder().token(self.config.telegram_token).build()
            logger.info("✅ Telegram Application создан")

            # 2. LiveTransparencyBroadcaster
            if self.config.enable_transparency:
                self.broadcaster = LiveTransparencyBroadcaster(telegram_app=self.app)
                self.broadcaster.start()  # Not async, no await needed
                logger.info("✅ LiveTransparencyBroadcaster запущен")

            # 3. UnifiedMemory
            self.memory = UnifiedMemory(
                workspace=self.config.workspace_path,
                persist_path=self.config.persist_path,
                max_history=self.config.max_message_history
            )
            logger.info("✅ UnifiedMemory инициализирована")

            # 4. SkillsIntegration
            if self.config.enable_skills and SKILLS_AVAILABLE:
                try:
                    skills_loader = SkillsLoader(workspace=self.config.workspace_path)
                    self.skills = SkillsIntegration(skills_loader=skills_loader)
                    result = self.skills.register_all_skills()
                    logger.info(f"✅ SkillsIntegration: {result['success']} навыков зарегистрировано, {result['failure']} ошибок")
                except Exception as e:
                    logger.warning(f"⚠️ SkillsIntegration не доступен: {e}")
                    self.skills = None

            # 5. AgentFactory
            if AGENT_FACTORY_AVAILABLE:
                try:
                    from core.config import Config
                    config = Config(config_path="meta-orchestrator.yaml")
                    self.agent_factory = AgentFactory(
                        config=config,
                        broadcaster=self.broadcaster,
                        unified_memory=self.memory
                    )
                    logger.info("✅ AgentFactory инициализирован")
                except Exception as e:
                    logger.warning(f"⚠️ AgentFactory не доступен: {e}")
                    self.agent_factory = None

            # 6. Регистрация обработчиков
            self._register_handlers()
            logger.info("✅ Обработчики команд зарегистрированы")

            # 7. Установка команд бота
            await self._setup_bot_commands()

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

        # Обработчик текстовых сообщений
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

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
        ]

        await self.app.bot.set_my_commands(commands)
        logger.info("Команды бота установлены")

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
                "/status - статус системы\n\n"
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
                "• /status - статистика работы системы\n\n"
                "<b>Особенности:</b>\n"
                "✨ Полная прозрачность - видны все действия агентов и подагентов\n"
                "💾 Гибридная память - краткосрочная + долгосрочная\n"
                "🎯 Навыки - агент знает специализированные инструкции\n"
                "🛡️ Устойчивость к ошибкам - 5 уровней защиты"
            )

            await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_help: {e}")
            await self._send_error_message(update, "Ошибка при обработке команды /help")

    async def cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /clear - очистить историю диалога"""
        try:
            if self.memory:
                self.memory.context_manager.clear_history()
                await update.message.reply_text(
                    "✅ <b>История диалога очищена</b>\n\n"
                    "Краткосрочная память сброшена.\n"
                    "Долгосрочная память (MEMORY.md) сохранена.",
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text("⚠️ Система памяти недоступна")

        except Exception as e:
            logger.error(f"Ошибка в cmd_clear: {e}")
            await self._send_error_message(update, "Ошибка при очистке памяти")

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /memory - показать содержимое памяти"""
        try:
            if not self.memory:
                await update.message.reply_text("⚠️ Система памяти недоступна")
                return

            # Получить полный контекст
            full_context = self.memory.get_full_context(last_n_messages=10)

            if not full_context or full_context.strip() == "":
                await update.message.reply_text("📭 Память пуста")
                return

            # Разбить на части если слишком длинное
            max_length = 4000
            if len(full_context) <= max_length:
                message = f"💾 <b>Содержимое памяти:</b>\n\n<tg-spoiler>{full_context}</tg-spoiler>"
                await update.message.reply_text(message, parse_mode=ParseMode.HTML)
            else:
                # Отправить по частям
                parts = [full_context[i:i+max_length] for i in range(0, len(full_context), max_length)]
                await update.message.reply_text(f"💾 <b>Содержимое памяти ({len(parts)} частей):</b>", parse_mode=ParseMode.HTML)
                for i, part in enumerate(parts, 1):
                    message = f"<b>Часть {i}/{len(parts)}:</b>\n\n<tg-spoiler>{part}</tg-spoiler>"
                    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

        except Exception as e:
            logger.error(f"Ошибка в cmd_memory: {e}")
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
                f"{'✅' if self.memory else '❌'} UnifiedMemory\n"
                f"{'✅' if self.skills else '❌'} SkillsIntegration\n"
                f"{'✅' if self.agent_factory else '❌'} AgentFactory\n"
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

            # Сохранить в память
            if self.memory:
                self.memory.add_message("user", message_text)

            # Запустить обработку в отдельной задаче
            task = asyncio.create_task(
                self._process_user_message(chat_id, message_text)
            )
            self.active_tasks[chat_id] = task

            # Очистить завершенную задачу
            task.add_done_callback(lambda t: self.active_tasks.pop(chat_id, None))

        except Exception as e:
            logger.error(f"Ошибка в handle_message: {e}")
            logger.error(traceback.format_exc())
            self.stats["errors_handled"] += 1
            await self._send_error_message(update, "Ошибка при обработке сообщения")

    async def _process_user_message(self, chat_id: int, message_text: str):
        """Обработка сообщения пользователя с запуском агента (Layer 3: Agent resilience)"""
        try:
            # Создать начальное сообщение прогресса
            if self.broadcaster:
                progress_msg_id = await self.broadcaster.create_progress_message(
                    chat_id=chat_id,
                    initial_text="🤖 Запускаю агента..."
                )

            # Запустить агента
            if not self.agent_factory:
                response = "⚠️ AgentFactory недоступен. Система работает в ограниченном режиме."
            else:
                self.stats["agents_launched"] += 1

                # Emit событие старта агента
                if self.broadcaster:
                    await self.broadcaster.emit_event(ProgressEvent(
                        event_type="agent_start",
                        agent_name="MainAgent",
                        content=f"Обработка запроса: {message_text[:100]}",
                        parent_id=None,
                        status="running",
                        timestamp=datetime.now().isoformat(),
                        details={"query": message_text, "chat_id": chat_id}
                    ))

                # Запуск агента через AgentFactory
                try:
                    # Получаем default агента из конфигурации
                    default_agent_key = self.agent_factory.config.get_default_agent()

                    # Запускаем агента с активным контекстом для диалога
                    response = await self.agent_factory.run_agent(
                        agent_key=default_agent_key,
                        message=message_text,
                        use_active_context=True  # Используем единый контекст для диалога
                    )
                except Exception as agent_error:
                    logger.error(f"Ошибка запуска агента: {agent_error}")
                    logger.error(traceback.format_exc())
                    response = f"❌ Ошибка при выполнении запроса:\n\n{str(agent_error)}"

                # Emit событие завершения агента
                if self.broadcaster:
                    await self.broadcaster.emit_event(ProgressEvent(
                        event_type="agent_end",
                        agent_name="MainAgent",
                        content="Обработка завершена",
                        parent_id=None,
                        status="completed",
                        timestamp=datetime.now().isoformat(),
                        details={"response_length": len(response)}
                    ))

            # Сохранить ответ в память
            if self.memory:
                self.memory.add_message("assistant", response)

            # Отправить финальный ответ отдельным сообщением
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>Ответ:</b>\n\n{response}",
                parse_mode=ParseMode.HTML
            )

        except Exception as e:
            logger.error(f"Ошибка в _process_user_message: {e}")
            logger.error(traceback.format_exc())
            self.stats["errors_handled"] += 1

            # Отправить сообщение об ошибке
            error_msg = f"❌ <b>Ошибка при обработке запроса:</b>\n\n<code>{str(e)}</code>"
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=error_msg,
                parse_mode=ParseMode.HTML
            )

    # ===== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ =====

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
        """Остановка бота"""
        try:
            logger.info("🛑 Остановка TelegramBridge...")

            # Дождаться завершения активных задач
            if self.active_tasks:
                logger.info(f"Ожидание завершения {len(self.active_tasks)} активных задач...")
                await asyncio.gather(*self.active_tasks.values(), return_exceptions=True)

            # Остановить broadcaster
            if self.broadcaster:
                self.broadcaster.stop()  # Not async, no await needed

            # Остановить Telegram
            if self.app:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()

            logger.info("✅ TelegramBridge остановлен")

        except Exception as e:
            logger.error(f"Ошибка при остановке TelegramBridge: {e}")
            logger.error(traceback.format_exc())
