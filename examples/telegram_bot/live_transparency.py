"""
Live Transparency System для отображения прогресса агентов в Telegram.

Ключевые фичи:
- Real-time обновления через edit_message_text
- Spoilers для длинного контента
- Rate limiting (max 1 edit/sec per message)
- Иерархическое отображение агентов/подагентов/инструментов
- Иконки статуса (⏳/✅/❌)
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any

from core.tracing.events import ProgressEvent

logger = logging.getLogger(__name__)


@dataclass
class ProgressMessageState:
    """Состояние progress message в Telegram"""
    chat_id: str
    message_id: str
    events: List[ProgressEvent] = field(default_factory=list)
    last_update: float = 0  # Timestamp последнего обновления
    is_finalized: bool = False


class LiveTransparencyBroadcaster:
    """
    Broadcaster для live-обновлений прогресса в Telegram.

    Собирает события от агентов, агрегирует их, форматирует как HTML
    с spoilers и обновляет сообщения в Telegram с rate limiting.
    """

    def __init__(self, telegram_app=None, show_tool_calls: bool = True, update_interval: float = 1.0):
        """
        Args:
            telegram_app: python-telegram-bot Application instance
            show_tool_calls: Показывать ли вызовы инструментов
            update_interval: Интервал обновления в секундах
        """
        self.app = telegram_app
        self.show_tool_calls = show_tool_calls
        self.progress_queue: asyncio.Queue[ProgressEvent] = asyncio.Queue()

        # Tracking active progress messages
        self.active_messages: Dict[str, ProgressMessageState] = {}

        # Rate limiting: настраиваемый интервал обновления
        self.MIN_EDIT_INTERVAL = update_interval

        # Buffer for pending updates
        self.pending_updates: Dict[str, List[ProgressEvent]] = {}

        # Background processor task
        self._processor_task: Optional[asyncio.Task] = None
        self._running = False

    def start(self):
        """Start background event processor"""
        if not self._running:
            self._running = True
            self._processor_task = asyncio.create_task(self._process_events())
            logger.info("LiveTransparencyBroadcaster started")

    def stop(self):
        """Stop background processor"""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
        logger.info("LiveTransparencyBroadcaster stopped")

    async def create_progress_message(
        self,
        chat_id: str,
        initial_text: str = "🤖 Обработка вашего запроса..."
    ) -> str:
        """
        Create initial progress message in Telegram.

        Args:
            chat_id: Telegram chat ID
            initial_text: Initial message text

        Returns:
            message_id as string
        """
        if not self.app:
            logger.warning("Telegram app not set, cannot create progress message")
            return "dummy_msg_id"

        try:
            msg = await self.app.bot.send_message(
                chat_id=int(chat_id),
                text=initial_text,
                parse_mode="HTML"
            )

            message_id = str(msg.message_id)
            self.active_messages[message_id] = ProgressMessageState(
                chat_id=chat_id,
                message_id=message_id,
                events=[],
                last_update=time.time()
            )

            logger.debug(f"Created progress message {message_id} in chat {chat_id}")
            return message_id

        except Exception as e:
            logger.error(f"Failed to create progress message: {e}")
            return "dummy_msg_id"

    async def emit_event(self, event: ProgressEvent):
        """Emit progress event (called by agents/tools)"""
        await self.progress_queue.put(event)

    async def finalize_progress_message(
        self,
        chat_id: str,
        message_id: str
    ):
        """Mark progress message as finalized (no more updates)"""
        state = self.active_messages.get(message_id)
        if state:
            state.is_finalized = True
            # Final update
            await self._update_message(state)
            # Clean up after a delay
            await asyncio.sleep(2)
            if message_id in self.active_messages:
                del self.active_messages[message_id]
            if message_id in self.pending_updates:
                del self.pending_updates[message_id]

    async def _process_events(self):
        """Background processor: consume events and update messages"""
        logger.info("Event processor started")

        while self._running:
            try:
                # Collect events with timeout to trigger periodic updates
                try:
                    event = await asyncio.wait_for(
                        self.progress_queue.get(),
                        timeout=0.5
                    )

                    # Add event to all active messages (simple approach)
                    # In production, you'd want to track which message owns which event
                    for msg_id, state in list(self.active_messages.items()):
                        if not state.is_finalized:
                            state.events.append(event)
                            if msg_id not in self.pending_updates:
                                self.pending_updates[msg_id] = []
                            self.pending_updates[msg_id].append(event)

                except asyncio.TimeoutError:
                    pass  # Trigger update check below

                # Update messages with pending events
                now = time.time()

                for msg_id in list(self.pending_updates.keys()):
                    state = self.active_messages.get(msg_id)
                    if not state:
                        continue

                    # Rate limit: only update if >1 sec since last edit
                    time_since_last = now - state.last_update

                    if time_since_last >= self.MIN_EDIT_INTERVAL:
                        await self._update_message(state)
                        state.last_update = now
                        self.pending_updates[msg_id] = []

            except Exception as e:
                logger.error(f"Event processor error: {e}", exc_info=True)
                await asyncio.sleep(1)

        logger.info("Event processor stopped")

    async def _update_message(self, state: ProgressMessageState):
        """Update Telegram message with current progress"""
        if not self.app:
            return

        try:
            formatted_text = self._format_progress_html(state.events)

            await self.app.bot.edit_message_text(
                chat_id=int(state.chat_id),
                message_id=int(state.message_id),
                text=formatted_text,
                parse_mode="HTML"
            )

            logger.debug(f"Updated progress message {state.message_id}")

        except Exception as e:
            # Common error: message not modified (same content)
            if "message is not modified" not in str(e).lower():
                logger.error(f"Failed to update progress message: {e}")

    def _format_progress_html(self, events: List[ProgressEvent]) -> str:
        """
        Format events as HTML with collapsible sections (spoilers).

        Uses Telegram HTML format with <tg-spoiler> tags for long content.
        """
        if not events:
            return "🤖 Ожидание событий..."

        # Build hierarchical structure
        root_events = [e for e in events if e.parent_id is None]

        lines = ["<b>🤖 Прогресс обработки:</b>\n"]

        for event in root_events:
            lines.append(self._format_event_tree(event, events, depth=0))

        result = "\n".join(lines)

        # Telegram has 4096 char limit for messages
        if len(result) > 4000:
            result = result[:4000] + "\n\n<i>... (truncated)</i>"

        return result

    def _format_event_tree(
        self,
        event: ProgressEvent,
        all_events: List[ProgressEvent],
        depth: int
    ) -> str:
        """Format single event and its children recursively"""
        indent = "  " * depth

        # Status icon
        icon = self._get_status_icon(event)

        # Event type icon
        type_icon = self._get_type_icon(event.event_type)

        # Event header
        header = f"{indent}{icon} {type_icon} <b>{event.agent_name}</b>"

        # Content handling - показываем полный текст без spoiler
        if event.content:
            # Ограничиваем длину для читаемости, но без блюра
            if len(event.content) > 500:
                truncated = event.content[:500] + "..."
                content_display = f": {truncated}"
            else:
                content_display = f": {event.content}"
        else:
            content_display = ""

        lines = [f"{header}{content_display}"]

        # Show details if available and status is completed
        if event.details and event.status == "completed":
            details_text = self._format_details(event.details)
            if details_text:
                # Ограничиваем длину деталей, но показываем полностью
                if len(details_text) > 300:
                    details_text = details_text[:300] + "..."
                lines.append(f"{indent}    📝 {details_text}")

        # Recursively add children
        children = [e for e in all_events if e.parent_id == event.agent_name]
        for child in children:
            lines.append(
                self._format_event_tree(child, all_events, depth + 1)
            )

        return "\n".join(lines)

    def _get_status_icon(self, event: ProgressEvent) -> str:
        """Get icon for event status"""
        return {
            "running": "⏳",
            "completed": "✅",
            "failed": "❌"
        }.get(event.status, "⚙️")

    def _get_type_icon(self, event_type: str) -> str:
        """Get icon for event type"""
        icons = {
            "agent_start": "🤖",
            "agent_end": "🤖",
            "tool_call_start": "🔧",
            "tool_call_end": "🔧",
            "tool_error": "🔧",
            "blackboard_post": "🧠",
            "subagent_spawn": "👶",
            "error": "❌"
        }
        return icons.get(event_type, "📌")

    def _format_details(self, details: Dict[str, Any]) -> str:
        """Format details dict as readable string"""
        if not details:
            return ""

        parts = []
        for key, value in details.items():
            if isinstance(value, str) and len(value) > 100:
                value = value[:100] + "..."
            parts.append(f"{key}: {value}")

        return ", ".join(parts)
