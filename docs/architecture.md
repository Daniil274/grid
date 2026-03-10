# Архитектура системы Grid

## Оглавление
1. [Обзор](#обзор)
2. [Высокоуровневая архитектура](#высокоуровневая-архитектура)
3. [Карта зависимостей](#карта-зависимостей)
4. [Основные компоненты](#основные-компоненты)
5. [Поток данных](#поток-данных)
6. [Интеграции](#интеграции)

---

## Обзор

**Grid** — модульная система для оркестрации агентов ИИ с расширенными возможностями инструментов, памяти и каналов коммуникации.

**Ключевые принципы:**
- **Модульность**: Инструменты, агенты и каналы подключаются через конфигурацию
- **Изоляция**: Docker/Podman для безопасности
- **Прозрачность**: Streaming событий и Telegram-уведомления
- **Память**: Гибридная (short-term сессии + long-term SQLite)
- **MCP-поддержка**: Стандартизированные инструменты (terminal, filesystem, git)

---

## Высокоуровневая архитектура

```
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│     User Input      │    │   Telegram Bridge   │    │   Voice Input       │
│   (CLI/API/Files)   │◄──►│ (channels/telegram) │◄──►│ (STT/TTS)          │
└─────────────────────┘    └─────────────────────┘    └─────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AgentFactory                                   │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐          │
│  │   Config    │ │ MemoryStore │ │Context Mgr  │ │ Skill Mgr   │          │
│  │(config.yaml)│ │ (SQLite)    │ │(dialog hist)│ │(skills/md)  │          │
│  └─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘          │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐                        │
│  │ContainerMgr │ │ MCP Servers │ │ Agent Cache │                        │
│  │ (Docker)    │ │ (stdio)     │ │ & Sessions  │                        │
│  └─────────────┘ └─────────────┘ └─────────────┘                        │
└─────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
        ┌────────────────────────────────────────┐
        │      OpenAI Agents SDK                 │
        │   Agent(name, model, tools, mcp)       │
        └────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │ Function     │ │ Agent Tools  │ │ MCP Tools     │
    │ Tools         │ │ (tools/)     │ │ (terminal,    │
    │ (orchestrator │ │              │ │  filesystem)  │
    │  memory, etc) │ │              │ │               │
    └─────────────┘ └─────────────┘ └──────────────┘
```

---

## Карта зависимостей

| Компонент | Зависимости | Использует |
|-----------|-------------|------------|
| **AgentFactory** | config.py, memory_store.py, context.py | OpenAI Agents SDK, tools/* |
| **Tools/** | agent_factory (GridRunContext) | filesystem, git_tools, vision_tools |
| **Channels/** | AgentFactory (emit_progress) | telegram_bridge.py, live_transparency.py |
| **Core/** | providers (OpenAI clients) | config.yaml, SQLite (memory) |
| **Telegram Bridge** | AgentFactory.run_agent | aiogram, asyncio |
| **MemoryStore** | SQLite | long/short-term memory |
| **MCP Servers** | npx @modelcontextprotocol/* | stdio subprocess |

**Граф зависимостей (упрощённый):**
```
config.yaml → AgentFactory → Agent (SDK)
tools/* → Agent.tools
channels/* → AgentFactory.broadcaster
docker → ContainerManager → GridRunContext
```

---

## Основные компоненты

### 1. AgentFactory (core/agent_factory.py)
Центральный оркестратор:
- Создание/кэширование агентов
- Управление сессиями и контекстом
- Streaming observers
- Интеграция MCP и function tools

### 2. Tools (tools/)
- **Function Tools**: Прямые вызовы (beads_tools, file_tools, git_tools)
- **Agent Tools**: Подагенты (orchestrator_tools, skill_tools)

### 3. Channels (channels/)
- **TelegramBridge**: Обработка сообщений, запуск агентов
- **LiveTransparency**: Прогресс-уведомления в реальном времени

### 4. Core Modules
| Модуль | Описание |
|--------|----------|
| config.py | Загрузка/валидация config.yaml |
| memory_store.py | SQLite long/short-term memory |
| context.py | История диалогов |
| pipeline_registry.py | Регистр пайплайнов |
| skills_integration.py | Навыки из Markdown |
| timeline_tracer.py | Трассировка |

### 5. Внешние зависимости
- **OpenAI Agents SDK**: Основной runtime агентов
- **MCP Servers**: npx пакеты (filesystem, terminal, git)
- **aiogram**: Telegram Bot API
- **Docker/Podman**: Изоляция

---

## Поток данных

### 1. Telegram → Agent
```
User Message (Telegram) → telegram_bridge.py → AgentFactory.run_agent() → Agent SDK → Tools → Response → Telegram
```

### 2. CLI/API → Agent
```
CLI Input → AgentFactory.run_agent() → Streaming (ConsoleObserver) → Output
```

### 3. Tool Call
```
Agent → Tool (e.g. git_tools.py) → GridRunContext.factory → Sub-agent/Tool → Result → Agent
```

### 4. Progress Emission
```
AgentFactory.emit_progress() → LiveTransparencyBroadcaster → Telegram Update
```

---

## Интеграции

- **Telegram**: Полный бот с voice, images, transparency
- **MCP**: Стандартизированные инструменты (10+ серверов)
- **Docker**: Изоляция рабочих пространств
- **Voice**: STT (Whisper), TTS (local models)
- **Vision**: OCR, image analysis
- **Beads**: Task management (workspace/beads)

---

*Архитектура составлена на основе анализа core/, tools/, channels/ (2026).*
