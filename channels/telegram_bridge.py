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
    enable_transparency: bool = True
    show_tool_calls: bool = True
    allowed_users: Optional[list] = None  # None = все пользователи
    max_concurrent_tasks_per_user: int = 1
    progress_update_interval: float = 1.0


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

        # Основные компоненты (глобальные)
        self.skills: Optional[SkillsIntegration] = None
        self.agent_factory: Optional[AgentFactory] = None

        # Изоляция пользователей: каждый user_id имеет свою память и workspace
        self.user_memories: Dict[int, UnifiedMemory] = {}  # user_id -> UnifiedMemory
        self.user_workspaces: Dict[int, Path] = {}  # user_id -> workspace_path

        # Состояние активных операций
        self.active_tasks: Dict[int, asyncio.Task] = {}  # chat_id -> task

        # Контекст для каждого чата (chat_id -> список сообщений)
        self.chat_contexts: Dict[int, list] = {}  # chat_id -> [{"role": "user/assistant", "content": str}]

        # Статистика (для команды /status)
        self.stats = {
            "start_time": datetime.now(),
            "messages_processed": 0,
            "agents_launched": 0,
            "errors_handled": 0,
        }

        logger.info("TelegramBridge инициализирован")

    def _get_user_workspace(self, user_id: int) -> Path:
        """
        Получить workspace пользователя для агентов, создать если не существует.

        Возвращает: workspace/user_{user_id}/workspace - директория для работы агентов
        """
        if user_id not in self.user_workspaces:
            # Рабочая директория агентов: workspace/user_{user_id}/workspace
            user_workspace = self.config.workspace_path / f"user_{user_id}" / "workspace"
            user_workspace.mkdir(parents=True, exist_ok=True)

            self.user_workspaces[user_id] = user_workspace
            logger.debug(f"Создан workspace для user_{user_id}: {user_workspace}")
        return self.user_workspaces[user_id]

    def _get_user_memory(self, user_id: int) -> UnifiedMemory:
        """
        Получить память пользователя, создать если не существует.

        Память располагается в workspace/user_{user_id}/ (MEMORY.md и daily_notes)
        """
        if user_id not in self.user_memories:
            # Базовая директория пользователя для памяти: workspace/user_{user_id}
            user_base_dir = self.config.workspace_path / f"user_{user_id}"
            user_base_dir.mkdir(parents=True, exist_ok=True)

            # Директория для persist (контекст)
            user_persist = self.config.persist_path / f"user_{user_id}"
            user_persist.mkdir(parents=True, exist_ok=True)

            # Создать UnifiedMemory для пользователя
            # workspace используется для MEMORY.md и daily_notes
            user_memory = UnifiedMemory(
                workspace=user_base_dir,  # workspace/user_{user_id}/ для MEMORY.md и daily_notes
                persist_path=user_persist,
                max_history=self.config.max_message_history
            )
            self.user_memories[user_id] = user_memory

            logger.info(f"Создана память для user_{user_id}: workspace={user_base_dir}, persist={user_persist}")

        return self.user_memories[user_id]

    async def initialize(self):
        """Инициализация всех компонентов (Layer 1: Connection resilience)"""
        try:
            logger.info("Запуск инициализации TelegramBridge...")

            # 1. Telegram Application
            self.app = Application.builder().token(self.config.telegram_token).build()
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
            # Примечание: UnifiedMemory передается per-user при запуске агента
            if AGENT_FACTORY_AVAILABLE:
                try:
                    from core.config import Config
                    # Используем основной config.yaml
                    config = Config(config_path="config.yaml")
                    self.agent_factory = AgentFactory(
                        config=config,
                        broadcaster=None,  # Отключаем live updates для избежания flood control
                        unified_memory=None  # Память будет передаваться per-user
                    )
                    logger.info("✅ AgentFactory инициализирован (без глобальной памяти)")
                except Exception as e:
                    logger.warning(f"⚠️ AgentFactory не доступен: {e}")
                    self.agent_factory = None

            # 4. Регистрация обработчиков
            self._register_handlers()
            logger.info("✅ Обработчики команд зарегистрированы")

            # 5. Установка команд бота
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
                "Долгосрочная память (MEMORY.md) сохранена.",
                parse_mode=ParseMode.HTML
            )

        except Exception as e:
            logger.error(f"Ошибка в cmd_clear: {e}")
            await self._send_error_message(update, "Ошибка при очистке памяти")

    async def cmd_memory(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Команда /memory - показать содержимое памяти"""
        try:
            user_id = update.effective_user.id
            user_memory = self._get_user_memory(user_id)

            # Получить полный контекст
            full_context = user_memory.get_full_context(last_n_messages=10)

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
            # Получить память пользователя
            user_memory = self._get_user_memory(user_id)

            # Получить рабочую директорию пользователя
            user_workspace = self._get_user_workspace(user_id)

            # Отправить одно простое сообщение о начале работы
            status_message = await self.app.bot.send_message(
                chat_id=chat_id,
                text="🤖 Обрабатываю запрос..."
            )

            # Запустить агента
            if not self.agent_factory:
                response = "⚠️ AgentFactory недоступен. Система работает в ограниченном режиме."
            else:
                self.stats["agents_launched"] += 1

                # Временно установить память пользователя для agent_factory
                self.agent_factory.unified_memory = user_memory

                # КРИТИЧЕСКИ ВАЖНО: Установить глобальную память для memory_tools
                # Это нужно делать перед каждым запуском агента, чтобы memory_tools
                # работали с памятью текущего пользователя
                from tools.memory_tools import set_unified_memory
                set_unified_memory(user_memory)
                logger.debug(f"Установлена память для memory_tools: user_{user_id}")

                # Установить рабочую директорию пользователя для agent_factory
                # Это нужно чтобы все операции с файлами выполнялись в workspace пользователя
                original_working_dir = self.agent_factory.config.get_working_directory()
                self.agent_factory.config.set_working_directory(str(user_workspace))
                logger.debug(f"Установлена рабочая директория для user_{user_id}: {user_workspace}")

                # Запуск агента через AgentFactory
                try:
                    # Получаем default агента из конфигурации
                    default_agent_key = self.agent_factory.config.get_default_agent()

                    # Получить историю контекста для этого чата
                    chat_history = self.chat_contexts.get(chat_id, [])

                    # Подготовить сообщение с контекстом если есть история
                    if len(chat_history) > 1:  # Есть история кроме текущего сообщения
                        # Формируем контекст из истории чата
                        context_text = "\n".join([
                            f"{'Пользователь' if msg['role'] == 'user' else 'Ассистент'}: {msg['content']}"
                            for msg in chat_history[:-1]  # Все кроме текущего сообщения
                        ])
                        message_with_context = f"[Контекст диалога]\n{context_text}\n\n[Текущий вопрос]\n{message_text}"
                    else:
                        message_with_context = message_text

                    # Запускаем агента с контекстом
                    response = await self.agent_factory.run_agent(
                        agent_key=default_agent_key,
                        message=message_with_context,
                        use_active_context=False  # Не используем глобальный контекст, у нас свой per-chat
                    )
                except Exception as agent_error:
                    logger.error(f"Ошибка запуска агента: {agent_error}")
                    logger.error(traceback.format_exc())
                    response = f"❌ Ошибка при выполнении запроса:\n\n{str(agent_error)}"
                finally:
                    # Восстановить оригинальную рабочую директорию
                    self.agent_factory.config.set_working_directory(original_working_dir)
                    logger.debug(f"Восстановлена исходная рабочая директория: {original_working_dir}")

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

            # Отправить финальный ответ отдельным сообщением с экранированием HTML
            escaped_response = self._escape_html(response)
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>Ответ:</b>\n\n{escaped_response}",
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

    def _escape_html(self, text: str) -> str:
        """Экранирование HTML для безопасной отправки в Telegram"""
        return html.escape(text)

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
            # Получить workspace пользователя (уже указывает на workspace/user_{user_id}/workspace)
            user_workspace = self._get_user_workspace(user_id)

            # Создать директорию для файлов если не существует
            # Теперь файлы будут в workspace/user_{user_id}/workspace/telegram_files
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

            document = update.message.document
            file_name = document.file_name
            file_size = document.file_size

            logger.info(f"Получен документ от {user_id}: {file_name} ({file_size} bytes)")

            # Проверка размера файла (ограничение 20MB)
            max_size = 20 * 1024 * 1024  # 20MB
            if file_size > max_size:
                await update.message.reply_text(
                    f"❌ Файл слишком большой ({file_size / 1024 / 1024:.2f} MB). "
                    f"Максимальный размер: {max_size / 1024 / 1024:.0f} MB"
                )
                return

            # Сохранить файл
            status_msg = await update.message.reply_text("📥 Скачиваю файл...")

            file_path, original_name = await self._save_file(document, user_id, "document")

            await status_msg.edit_text(
                f"✅ Файл получен и сохранен:\n\n"
                f"📄 <b>Имя:</b> <code>{original_name}</code>\n"
                f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                f"Файл доступен для обработки агентами.",
                parse_mode=ParseMode.HTML
            )

            # Добавить информацию о файле в память пользователя
            user_memory = self._get_user_memory(user_id)
            user_memory.add_message(
                "system",
                f"Пользователь отправил файл: {original_name} (путь: {file_path})"
            )

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

            # Получить самое большое фото из массива
            photo = update.message.photo[-1]
            file_size = photo.file_size

            logger.info(f"Получено фото от {user_id} ({file_size} bytes)")

            # Сохранить файл
            status_msg = await update.message.reply_text("📥 Скачиваю фото...")

            file_path, file_name = await self._save_file(photo, user_id, "photo")

            await status_msg.edit_text(
                f"✅ Фото получено и сохранено:\n\n"
                f"🖼️ <b>Файл:</b> <code>{file_name}</code>\n"
                f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB\n\n"
                f"Фото доступно для анализа агентами.",
                parse_mode=ParseMode.HTML
            )

            # Добавить в память
            user_memory = self._get_user_memory(user_id)
            user_memory.add_message(
                "system",
                f"Пользователь отправил фото (путь: {file_path})"
            )

        except Exception as e:
            logger.error(f"Ошибка при обработке фото: {e}")
            logger.error(traceback.format_exc())
            await self._send_error_message(update, "Ошибка при обработке фото")

    async def handle_audio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка аудио файлов"""
        try:
            user_id = update.effective_user.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            audio = update.message.audio
            file_name = audio.file_name or "audio.mp3"
            file_size = audio.file_size

            logger.info(f"Получено аудио от {user_id}: {file_name}")

            status_msg = await update.message.reply_text("📥 Скачиваю аудио...")

            file_path, original_name = await self._save_file(audio, user_id, "audio")

            await status_msg.edit_text(
                f"✅ Аудио получено и сохранено:\n\n"
                f"🎵 <b>Файл:</b> <code>{original_name}</code>\n"
                f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB",
                parse_mode=ParseMode.HTML
            )

            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("system", f"Пользователь отправил аудио: {original_name}")

        except Exception as e:
            logger.error(f"Ошибка при обработке аудио: {e}")
            await self._send_error_message(update, "Ошибка при обработке аудио")

    async def handle_video(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка видео файлов"""
        try:
            user_id = update.effective_user.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            video = update.message.video
            file_name = video.file_name or "video.mp4"
            file_size = video.file_size

            logger.info(f"Получено видео от {user_id}: {file_name}")

            # Проверка размера (видео может быть большим)
            max_size = 50 * 1024 * 1024  # 50MB
            if file_size > max_size:
                await update.message.reply_text(
                    f"❌ Видео слишком большое ({file_size / 1024 / 1024:.2f} MB). "
                    f"Максимальный размер: {max_size / 1024 / 1024:.0f} MB"
                )
                return

            status_msg = await update.message.reply_text("📥 Скачиваю видео...")

            file_path, original_name = await self._save_file(video, user_id, "video")

            await status_msg.edit_text(
                f"✅ Видео получено и сохранено:\n\n"
                f"🎬 <b>Файл:</b> <code>{original_name}</code>\n"
                f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                f"📊 <b>Размер:</b> {file_size / 1024 / 1024:.2f} MB",
                parse_mode=ParseMode.HTML
            )

            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("system", f"Пользователь отправил видео: {original_name}")

        except Exception as e:
            logger.error(f"Ошибка при обработке видео: {e}")
            await self._send_error_message(update, "Ошибка при обработке видео")

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обработка голосовых сообщений"""
        try:
            user_id = update.effective_user.id

            if not self._check_access(user_id):
                await update.message.reply_text("⛔ Доступ запрещен")
                return

            voice = update.message.voice
            file_size = voice.file_size

            logger.info(f"Получено голосовое сообщение от {user_id}")

            status_msg = await update.message.reply_text("📥 Скачиваю голосовое сообщение...")

            file_path, file_name = await self._save_file(voice, user_id, "voice")

            await status_msg.edit_text(
                f"✅ Голосовое сообщение получено:\n\n"
                f"🎤 <b>Файл:</b> <code>{file_name}</code>\n"
                f"📂 <b>Путь:</b> <code>{file_path}</code>\n"
                f"📊 <b>Размер:</b> {file_size / 1024:.2f} KB",
                parse_mode=ParseMode.HTML
            )

            user_memory = self._get_user_memory(user_id)
            user_memory.add_message("system", f"Пользователь отправил голосовое сообщение")

        except Exception as e:
            logger.error(f"Ошибка при обработке голосового сообщения: {e}")
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
