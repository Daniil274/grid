from types import SimpleNamespace

import pytest

from examples.telegram_bot.telegram_progress_observer import TelegramProgressObserver


class DummyBot:
    def __init__(self) -> None:
        self.calls = []

    async def edit_message_text(self, **kwargs):
        self.calls.append(kwargs)


def _tool_event(name: str, *, tool_name: str, arguments=None, output=None, server_label=None):
    raw_item = SimpleNamespace(
        name=tool_name,
        arguments=arguments,
        server_label=server_label,
    )
    item = SimpleNamespace(raw_item=raw_item, output=output)
    return SimpleNamespace(name=name, item=item)


@pytest.mark.asyncio
async def test_progress_observer_renders_compact_tool_flow():
    bot = DummyBot()
    observer = TelegramProgressObserver(
        bot=bot,
        chat_id=123,
        message_id=456,
        agent_label="kimi_engineer",
        update_interval=0.2,
        show_tool_calls=True,
    )

    observer.handle_event(
        _tool_event(
            "tool_called",
            tool_name="grep_tool",
            server_label="fs",
            arguments={"query": "telegram", "limit": 3},
        ),
        agent_key="kimi_engineer",
    )

    await observer.flush(force=True)
    assert bot.calls, "observer should edit the status message"
    first_text = bot.calls[-1]["text"]
    assert "grep_tool" in first_text
    assert "tools: <b>0</b>" in first_text
    assert "Последние шаги" in first_text

    observer.handle_event(
        _tool_event(
            "tool_output",
            tool_name="grep_tool",
            server_label="fs",
            output="found 8 matches in telegram_bridge.py",
        ),
        agent_key="kimi_engineer",
    )
    await observer.close(final_status="completed")

    final_text = bot.calls[-1]["text"]
    assert "tools: <b>1</b>" in final_text
    assert "готово" in final_text
    assert "found 8 matches" in final_text


@pytest.mark.asyncio
async def test_progress_observer_can_hide_tool_steps():
    bot = DummyBot()
    observer = TelegramProgressObserver(
        bot=bot,
        chat_id=1,
        message_id=2,
        agent_label="agent",
        update_interval=0.2,
        show_tool_calls=False,
    )

    observer.handle_event(
        _tool_event(
            "tool_called",
            tool_name="bash_tool",
            arguments={"command": "pytest"},
        ),
        agent_key="agent",
    )
    await observer.flush(force=True)

    text = bot.calls[-1]["text"]
    assert "Последние шаги" not in text
    assert "Запускаю bash_tool" in text
