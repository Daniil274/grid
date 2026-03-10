# Каналы интеграции (Channels)

## Оглавление
1. [Обзор](#обзор)
2. [Telegram Bridge](../examples/telegram_bot/telegram_bridge.py)
3. [Live Transparency](../examples/telegram_bot/live_transparency.py)
4. [Конфигурация](#конфигурация)
5. [Примеры использования](#примеры-использования)

---

## Обзор

**Channels** — модули для внешних интерфейсов:
- `telegram_bridge.py` (91KB): Полноценный Telegram-бот
- `live_transparency.py` (12KB): Прозрачность выполнения (прогресс)

**Интеграция:** Через `AgentFactory(broadcaster=...)` и Telegram config.

---

## Telegram Bridge (telegram_bridge.py)

**Функции:**
- Polling сообщений (text, voice, images)
- Запуск `AgentFactory.run_agent()`
- Multimodal: изображения → vision_agent
- Voice: STT → text → TTS → voice reply
- Команды: /memory, /workspace
- Ограничения: allowed_users, max_concurrent_tasks
- Transparency: Показ шагов (если `enable_transparency: true`)

**Конфигурация (config.yaml):**
| Параметр | Описание |
|----------|----------|
| `telegram.token_env` | TELEGRAM_BOT_TOKEN |
| `telegram.workspace_path` | ./workspace |
| `telegram.enable_transparency` | Показывать tool calls |
| `telegram.allowed_users` | Список user_id |

**Поток:**
```
User → Telegram Message/Voice/Image → Bridge → AgentFactory → Response → SendMessage/Voice
```

**Пример запуска:**
```python
from examples.telegram_bot.telegram_bridge import TelegramBridge
bridge = TelegramBridge(config)
await bridge.start_polling()
```

---

## Live Transparency (live_transparency.py)

**Функции:**
- Broadcasting прогресс-ивентов из `AgentFactory.emit_progress()`
- Telegram-уведомления в реальном времени
- Дерево задач (parent_id)
- Спойлеры для деталей (tool output)

**События:**
| Event | Описание |
|-------|----------|
| agent_start/end | Запуск/завершение агента |
| tool_call/output | Вызов/результат инструмента |
| handoff | Передача между агентами |

**Интеграция:**
```python
from examples.telegram_bot.live_transparency import LiveTransparencyBroadcaster
broadcaster = LiveTransparencyBroadcaster(channel="telegram_progress")
factory = AgentFactory(broadcaster=broadcaster)
```

**Конфигурация:**
- `telegram.enable_transparency: true`
- `progress_update_interval: 2.0`

**Пример события:**
```python
await factory.emit_progress(
    event_type="tool_call",
    agent_name="git_agent",
    content="git status",
    details={"output": "clean"}
)
```

---

## Конфигурация

**config.yaml (telegram секция):**
```yaml
telegram:
  token_env: TELEGRAM_BOT_TOKEN
  enable_transparency: true
  show_tool_calls: true
  max_concurrent_tasks_per_user: 1
```

---

## Примеры использования

### 1. Запуск бота
```bash
TELEGRAM_BOT_TOKEN=your_token python -m examples.telegram_bot.telegram_server
```

### 2. Transparency в AgentFactory
```python
factory = AgentFactory(broadcaster=LiveTransparencyBroadcaster())
response = await factory.run_agent("chat_agent", "Задача...")
# Автоматически шлёт прогресс в Telegram
```

---

*Документация на основе examples/telegram_bot/.*
