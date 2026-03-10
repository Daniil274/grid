# Документация Grid

## Оглавление
1. [Введение в проект](#введение-в-проект)
2. [Agent Factory](agent_factory.md)
3. [Конфигурация](config.md)
4. [Архитектура системы](architecture.md)
5. [Обзор инструментов](tools_overview.md)
6. [Каналы интеграции](channels.md)
7. [Emergency Shutdown](emergency_shutdown.md)

---

## Введение в проект

**Grid** — фреймворк для создания и управления автономными агентами на базе OpenAI Agents SDK с поддержкой:
- MCP (Model Context Protocol) инструментов
- Docker-изоляции
- Telegram-интеграции
- Голосового ввода/вывода
- Расширенной памяти (SQLite)
- Трассировки и прозрачности выполнения

**Основные компоненты:**
- **Core**: AgentFactory, Config, MemoryStore
- **Tools**: Функциональные и агентские инструменты (filesystem, git, vision и др.)
- **Channels**: Telegram Bridge, Live Transparency
- **Skills**: Интеграция навыков из Markdown

**Репозиторий:** [GitHub](https://github.com/your-org/grid) (гипотетическая ссылка)

---

*Документация автоматически сгенерирована Technical Writer.*
