# Integration Channels

## Table of Contents
1. [Overview](#overview)
2. [Telegram Bridge](../examples/telegram_bot/telegram_bridge.py)
3. [Live Transparency](../examples/telegram_bot/live_transparency.py)
4. [Configuration](#configuration)
5. [Usage Examples](#usage-examples)

---

## Overview

**Channels** — modules for external interfaces:
- `telegram_bridge.py` (91KB): Full-featured Telegram bot
- `live_transparency.py` (12KB): Execution transparency (progress)

**Integration:** Via `AgentFactory(broadcaster=...)` and Telegram config.

---

## Telegram Bridge (telegram_bridge.py)

**Functions:**
- Polling messages (text, voice, images)
- Running `AgentFactory.run_agent()`
- Multimodal: images → vision_agent
- Voice: STT → text → TTS → voice reply
- Commands: /memory, /workspace
- Restrictions: allowed_users, max_concurrent_tasks
- Transparency: Show steps (if `enable_transparency: true`)

**Configuration (config.yaml):**
| Parameter | Description |
|----------|----------|
| `telegram.token_env` | TELEGRAM_BOT_TOKEN |
| `telegram.workspace_path` | ./workspace |
| `telegram.enable_transparency` | Show tool calls |
| `telegram.allowed_users` | List of user_id |

**Flow:**
```
User → Telegram Message/Voice/Image → Bridge → AgentFactory → Response → SendMessage/Voice
```

**Launch Example:**
```python
from examples.telegram_bot.telegram_bridge import TelegramBridge
bridge = TelegramBridge(config)
await bridge.start_polling()
```

---

## Live Transparency (live_transparency.py)

**Functions:**
- Broadcasting progress events from `AgentFactory.emit_progress()`
- Real-time Telegram notifications
- Task tree (parent_id)
- Spoilers for details (tool output)

**Events:**
| Event | Description |
|-------|----------|
| agent_start/end | Agent start/completion |
| tool_call/output | Tool call/result |
| handoff | Agent handoff |

**Integration:**
```python
from examples.telegram_bot.live_transparency import LiveTransparencyBroadcaster
broadcaster = LiveTransparencyBroadcaster(channel="telegram_progress")
factory = AgentFactory(broadcaster=broadcaster)
```

**Configuration:**
- `telegram.enable_transparency: true`
- `progress_update_interval: 2.0`

**Event Example:**
```python
await factory.emit_progress(
    event_type="tool_call",
    agent_name="git_agent",
    content="git status",
    details={"output": "clean"}
)
```

---

## Configuration

**config.yaml (telegram section):**
```yaml
telegram:
  token_env: TELEGRAM_BOT_TOKEN
  enable_transparency: true
  show_tool_calls: true
  max_concurrent_tasks_per_user: 1
```

---

## Usage Examples

### 1. Launching the Bot
```bash
TELEGRAM_BOT_TOKEN=your_token python -m examples.telegram_bot.telegram_server
```

### 2. Transparency in AgentFactory
```python
factory = AgentFactory(broadcaster=LiveTransparencyBroadcaster())
response = await factory.run_agent("chat_agent", "Task...")
# Automatically sends progress to Telegram
```

---

*Documentation based on examples/telegram_bot/.*
