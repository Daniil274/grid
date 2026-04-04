from __future__ import annotations

import asyncio
import html
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from telegram.error import BadRequest

from core.agent_factory import ConsoleStreamObserver


@dataclass
class _Step:
    text: str
    status: str = "done"


class CompositeStreamObserver:
    """Fan-out observer that preserves the first returned text fragment."""

    def __init__(self, *observers: Any) -> None:
        self._observers = [observer for observer in observers if observer is not None]

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        fragment: Optional[str] = None
        for observer in self._observers:
            current = observer.handle_event(event, agent_key=agent_key)
            if fragment is None and current:
                fragment = current
        return fragment


class TelegramProgressObserver:
    """
    Compact CLI-like live status for Telegram.

    Keeps a single Telegram message updated with:
    - current phase
    - recent tool / handoff steps
    - counters

    This avoids chat flood while still showing what the agent is doing.
    """

    def __init__(
        self,
        *,
        bot: Any,
        chat_id: int,
        message_id: int,
        agent_label: str,
        update_interval: float = 2.0,
        show_tool_calls: bool = True,
        max_visible_steps: int = 6,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.agent_label = agent_label
        self.update_interval = max(0.2, float(update_interval))
        self.show_tool_calls = show_tool_calls
        self.max_visible_steps = max(3, int(max_visible_steps))

        self.started_at = time.monotonic()
        self.last_edit_at = 0.0
        self.last_rendered_text: Optional[str] = None
        self.last_output_preview: str = ""

        self.tokens_seen = False
        self.completed_tools = 0
        self.handoffs = 0
        self.current_action = "Анализирую задачу"
        self.steps: list[_Step] = []

        self._closed = False
        self._flush_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        if self._closed:
            return self._extract_text_fragment(event)

        event_name = getattr(event, "name", "")
        item = getattr(event, "item", None)
        raw_item = getattr(item, "raw_item", None) if item is not None else None

        if event_name == "tool_called" and raw_item is not None:
            tool_display = self._tool_display_name(raw_item)
            args_preview = self._format_args(getattr(raw_item, "arguments", None))
            self.current_action = f"Запускаю {tool_display}"
            if self.show_tool_calls:
                suffix = f" ({args_preview})" if args_preview else ""
                self._push_step(f"🔧 {tool_display}{suffix}", status="running")
            self._schedule_flush()
            return None

        if event_name == "tool_output" and item is not None:
            tool_display = self._tool_display_name(raw_item)
            output = getattr(item, "output", None)
            output_preview = self._preview_output(output)
            self.current_action = "Обрабатываю результаты инструментов"
            self.completed_tools += 1
            self.last_output_preview = output_preview
            if self.show_tool_calls:
                suffix = f" -> {output_preview}" if output_preview else ""
                self._push_step(f"✅ {tool_display}{suffix}", status="done")
            self._schedule_flush()
            return None

        if event_name == "handoff_requested" and raw_item is not None:
            target = getattr(raw_item, "name", None) or "agent"
            self.handoffs += 1
            self.current_action = f"Передаю работу агенту {target}"
            self._push_step(f"🔀 handoff -> {target}", status="running")
            self._schedule_flush()
            return None

        if event_name == "handoff_occured" and item is not None:
            src_agent = getattr(item, "source_agent", None)
            dst_agent = getattr(item, "target_agent", None)
            src_name = getattr(src_agent, "name", None) or agent_key or self.agent_label
            dst_name = getattr(dst_agent, "name", None) or "agent"
            self.current_action = f"Работает {dst_name}"
            self._push_step(f"✅ {src_name} => {dst_name}", status="done")
            self._schedule_flush()
            return None

        fragment = self._extract_text_fragment(event)
        if fragment:
            self.tokens_seen = True
            if self.completed_tools > 0:
                self.current_action = "Формулирую ответ"
            else:
                self.current_action = "Думаю над ответом"
            self._schedule_flush()
            return fragment

        return None

    async def close(self, *, final_status: str = "completed") -> None:
        self._closed = True
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush(force=True, final_status=final_status)

    async def flush(self, *, force: bool = False, final_status: Optional[str] = None) -> None:
        async with self._lock:
            text = self._render(final_status=final_status)
            if not force and text == self.last_rendered_text:
                return
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode="HTML",
                )
                self.last_rendered_text = text
                self.last_edit_at = time.monotonic()
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise

    def _schedule_flush(self) -> None:
        if self._closed:
            return
        if self._flush_task and not self._flush_task.done():
            return
        delay = max(0.0, self.update_interval - (time.monotonic() - self.last_edit_at))
        self._flush_task = asyncio.create_task(self._delayed_flush(delay))

    async def _delayed_flush(self, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self.flush()
        except asyncio.CancelledError:
            return
        finally:
            self._flush_task = None

    def _push_step(self, text: str, *, status: str = "done") -> None:
        if self.steps and self.steps[-1].text == text and self.steps[-1].status == status:
            return
        self.steps.append(_Step(text=text, status=status))
        if len(self.steps) > 40:
            self.steps = self.steps[-40:]

    def _render(self, *, final_status: Optional[str] = None) -> str:
        elapsed = int(max(0, time.monotonic() - self.started_at))
        is_done = final_status in {"completed", "failed"}
        icon = "✅" if final_status == "completed" else "❌" if final_status == "failed" else "🤖"
        header = "Завершено" if final_status == "completed" else "Ошибка" if final_status == "failed" else "Выполняю"

        lines = [
            f"{icon} <b>{header}: {html.escape(self.agent_label)}</b>",
            f"⏱ <b>{elapsed}s</b> · ⚙️ tools: <b>{self.completed_tools}</b> · 🔀 handoffs: <b>{self.handoffs}</b>",
        ]

        if not is_done:
            lines.append(f"Сейчас: <code>{html.escape(self.current_action)}</code>")
        elif final_status == "completed":
            lines.append("Состояние: <code>готово</code>")
        else:
            lines.append("Состояние: <code>завершилось с ошибкой</code>")

        if self.steps:
            lines.append("")
            lines.append("<b>Последние шаги:</b>")
            recent_steps = self.steps[-self.max_visible_steps :]
            hidden_count = max(0, len(self.steps) - len(recent_steps))
            if hidden_count:
                lines.append(f"• … ещё {hidden_count} шаг(ов)")
            for step in recent_steps:
                lines.append(f"• {html.escape(step.text)}")
        elif not is_done:
            lines.append("")
            lines.append("• Подготавливаю окружение")

        if self.last_output_preview and (is_done or self.completed_tools > 0):
            lines.append("")
            lines.append(f"<b>Последний результат:</b> {html.escape(self.last_output_preview)}")

        if self.tokens_seen and not is_done:
            lines.append("")
            lines.append("<i>Ответ уже генерируется…</i>")

        text = "\n".join(lines)
        if len(text) <= 4000:
            return text

        trimmed_lines: list[str] = []
        for line in lines:
            candidate = "\n".join([*trimmed_lines, line])
            if len(candidate) > 3900:
                break
            trimmed_lines.append(line)
        trimmed_lines.append("")
        trimmed_lines.append("<i>… вывод сокращён</i>")
        return "\n".join(trimmed_lines)

    @staticmethod
    def _tool_display_name(raw_item: Any) -> str:
        if raw_item is None:
            return "tool"
        tool_name = getattr(raw_item, "name", None) or getattr(raw_item, "type", None) or "tool"
        server_label = getattr(raw_item, "server_label", None)
        return f"{server_label}.{tool_name}" if server_label else str(tool_name)

    @staticmethod
    def _format_args(arguments: Any) -> str:
        value = ConsoleStreamObserver._format_args(arguments)
        value = " ".join(str(value).split())
        return value[:120] + ("..." if len(value) > 120 else "")

    @staticmethod
    def _preview_output(output: Any) -> str:
        if output is None:
            return ""
        if isinstance(output, dict):
            parts = []
            for key, value in list(output.items())[:3]:
                preview = str(value)
                preview = " ".join(preview.split())
                if len(preview) > 60:
                    preview = preview[:60] + "..."
                parts.append(f"{key}={preview}")
            return ", ".join(parts)
        text = " ".join(str(output).split())
        return text[:140] + ("..." if len(text) > 140 else "")

    @staticmethod
    def _extract_text_fragment(event: Any) -> Optional[str]:
        for candidate in _iter_text_candidates(event):
            if candidate and candidate.strip():
                return candidate
        return None


def _iter_text_candidates(event: Any) -> Iterable[Optional[str]]:
    yield getattr(event, "content", None)
    yield getattr(event, "delta", None)
    yield getattr(event, "text", None)

    data = getattr(event, "data", None)
    if data is None:
        return

    yield getattr(data, "content", None)
    yield getattr(data, "delta", None)
    yield getattr(data, "text", None)

    if isinstance(data, dict):
        yield data.get("content")
        yield data.get("delta")
        yield data.get("text")
