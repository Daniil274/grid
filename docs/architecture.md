# Grid System Architecture

## Table of Contents
1. [Overview](#overview)
2. [High-Level Architecture](#high-level-architecture)
3. [Dependency Map](#dependency-map)
4. [Main Components](#main-components)
5. [Data Flow](#data-flow)
6. [Integrations](#integrations)

---

## Overview

**Grid** is a modular system for orchestrating AI agents with advanced tools, memory, and communication channels.

**Key Principles:**
- **Modularity**: Tools, agents, and channels are connected via configuration
- **Isolation**: Docker/Podman for security
- **Transparency**: Streaming events and Telegram notifications
- **Memory**: Hybrid (short-term sessions + long-term SQLite)
- **MCP Support**: Standardized tools (terminal, filesystem, git)

---

## High-Level Architecture

```
┌─────────────────────┐    ┌─────────────────────┐    ┌─────────────────────┐
│     User Input      │    │   Telegram Bridge   │    │   Voice Input       │
│   (CLI/API/Files)   │◄──►│ (examples/telegram) │◄──►│ (STT/TTS)          │
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

## Dependency Map

| Component | Dependencies | Uses |
|-----------|-------------|------------|
| **AgentFactory** | config.py, memory_store.py, context.py | OpenAI Agents SDK, tools/* |
| **Tools/** | agent_factory (GridRunContext) | filesystem, git_tools, vision_tools |
| **Channels/** | AgentFactory (emit_progress) | telegram_bridge.py, live_transparency.py |
| **Core/** | providers (OpenAI clients) | config.yaml, SQLite (memory) |
| **Telegram Bridge** | AgentFactory.run_agent | aiogram, asyncio |
| **MemoryStore** | SQLite | long/short-term memory |
| **MCP Servers** | npx @modelcontextprotocol/* | stdio subprocess |

**Dependency Graph (simplified):**
```
config.yaml → AgentFactory → Agent (SDK)
tools/* → Agent.tools
examples/telegram_bot/* → AgentFactory.broadcaster
docker → ContainerManager → GridRunContext
```

---

## Main Components

### 1. AgentFactory (core/agent_factory.py)
Central orchestrator:
- Creating/caching agents
- Session and context management
- Streaming observers
- MCP and function tools integration

### 2. Tools (tools/)
- **Function Tools**: Direct calls (beads_tools, file_tools, git_tools)
- **Agent Tools**: Sub-agents (orchestrator_tools, skill_tools)

### 3. Channels (examples/telegram_bot/)
- **TelegramBridge**: Message processing, agent launching
- **LiveTransparency**: Real-time progress notifications

### 4. Core Modules
| Module | Description |
|--------|----------|
| config.py | Loading/validating config.yaml |
| memory_store.py | SQLite long/short-term memory |
| context.py | Dialog history |
| pipeline_registry.py | Pipeline registry |
| skills_integration.py | Skills from Markdown |
| timeline_tracer.py | Tracing |

### 5. External Dependencies
- **OpenAI Agents SDK**: Core agent runtime
- **MCP Servers**: npx packages (filesystem, terminal, git)
- **aiogram**: Telegram Bot API
- **Docker/Podman**: Isolation

---

## Data Flow

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

## Integrations

- **Telegram**: Full bot with voice, images, transparency
- **MCP**: Standardized tools (10+ servers)
- **Docker**: Workspace isolation
- **Voice**: STT (Whisper), TTS (local models)
- **Vision**: OCR, image analysis
- **Beads**: Task management (workspace/beads)

---

*Architecture compiled based on analysis of core/, tools/, examples/telegram_bot/ (2026).*
