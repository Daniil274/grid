# Agent Factory

## Оглавление
1. [Обзор](#обзор)
2. [Архитектура](#архитектура)
3. [Классы](#классы)
   - [StreamObserver](#streamobserver-protocol)
   - [ConsoleStreamObserver](#consolestreamobserver)
   - [AutoRunToolContext](#autoruntoolcontext)
   - [GridRunContext](#gridruncontext)
   - [AgentFactory](#agentfactory)
4. [Методы AgentFactory](#методы-agentfactory)
5. [Примеры использования](#примеры-использования)
6. [Интеграции](#интеграции)

---

## Обзор

**Agent Factory** — центральный компонент системы Grid, отвечающий за создание, конфигурацию и управление агентами на базе OpenAI Agents SDK.

**Основные обязанности:**
- Создание агентов из конфигурации (`config.yaml`) или динамически
- Кэширование агентов для повторного использования
- Управление контекстом диалога и сессиями памяти
- Интеграция с MCP-серверами (Model Context Protocol)
- Трассировка и логирование выполнения агентов
- Поддержка streaming-событий в реальном времени
- Интеграция с Telegram для прогресс-уведомлений

**Файл:** `core/agent_factory.py` (2572 строки)

---

## Архитектура

```
┌─────────────────────────────────────────────────────────────────┐
│                        AgentFactory                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐    │
│  │Config       │  │ContextManager│  │MemoryStore (SQLite) │    │
│  │(config.yaml)│  │(dialog history)│ │(long/short term memory)│ │
│  └─────────────┘  └──────────────┘  └─────────────────────┘    │
│                                                                 │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐    │
│  │SkillManager │  │ContainerMgr  │  │MCP Server Manager   │    │
│  │(skills/md)  │  │(Docker iso)  │  │(stdio MCP servers)  │    │
│  └─────────────┘  └──────────────┘  └─────────────────────┘    │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │           Agent Cache & Session Management              │   │
│  │  _agent_cache: Dict[str, Agent]                         │   │
│  │  _agent_sessions: Dict[tuple, SQLiteSession]            │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
        ┌────────────────────────────────────────┐
        │   OpenAI Agents SDK                    │
        │   ┌─────────────────────────────────┐  │
        │   │ Agent (name, instructions,      │  │
        │   │        model, tools, mcp)       │  │
        │   └─────────────────────────────────┘  │
        └────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │Function Tools│ │Agent Tools   │ │MCP Servers   │
    │(filesystem,  │ │(orchestrator,│ │(terminal,    │
    │ terminal,    │ │ task_analyst)│ │ filesystem,  │
    │ memory, etc) │ │              │ │ git, etc)    │
    └──────────────┘ └──────────────┘ └──────────────┘
```

---

## Классы

### StreamObserver (Protocol)

Протокол для компонентов, обрабатывающих потоковые события агентов.

```python
class StreamObserver(Protocol):
    """Protocol for components that render streaming events."""

    def handle_event(self, event: Any, *, agent_key: Optional[str] = None) -> Optional[str]:
        """Render a streaming event. Return text fragments to append to buffers if any."""
```

**Назначение:** Определяет интерфейс для наблюдателей за streaming-событиями (вызовы инструментов, вывод, handoff между агентами).

**Возвращает:** Текстовые фрагменты для добавления в буфер ответа (опционально).

---

### ConsoleStreamObserver

Реализация `StreamObserver` для вывода событий в консоль.

**Основные события:**
- `tool_called` — вызов инструмента (логотип 🔧)
- `tool_output` — результат инструмента (логотип ✅)
- `handoff_requested` — запрос передачи другому агенту (логотип 🔀)
- `handoff_occured` — факт передачи (логотип 🔁)
- `mcp_list_tools` — список MCP-инструментов (логотип 🧩)
- `RawResponsesStreamEvent` — текстовый ответ модели

**Пример использования:**
```python
observer = ConsoleStreamObserver(output_writer=print)
# Передаётся в AgentFactory через stream_observer параметр
factory = AgentFactory(stream_observer=observer)
```

---

### AutoRunToolContext

Вспомогательный класс для эмуляции `ToolContext` SDK при авто-запуске инструментов.

```python
class AutoRunToolContext:
    """Mock context that mimics SDK's ToolContext for direct tool invocation."""
    def __init__(self, context: Any, tool_name: str = ""):
        self.context = context
        self.tool_name = tool_name
```

**Назначение:** Позволяет запускать инструменты напрямую (без полного Runner) для инициализации агента (например, `beads_init`, `beads_ready`).

---

### GridRunContext

Контекст выполнения, передаваемый в OpenAI Agents SDK Runner.

```python
@dataclass
class GridRunContext:
    """
    Runtime context object passed into Agents SDK Runner.
    """
    factory: "AgentFactory"
    context_id: Optional[str] = None
    session: Optional[Any] = None  # SQLiteSession for local agent history
    user_id: Optional[str] = None  # User identifier for workspace isolation
    agent_id: Optional[str] = None  # Agent identifier for isolation
    metadata: Optional[dict] = None  # Additional metadata from context manager
    container_id: Optional[str] = None  # Docker container ID for isolation
```

**Поля:**
| Поле | Тип | Описание |
|------|-----|----------|
| `factory` | AgentFactory | Ссылка на фабрику для доступа инструментов |
| `context_id` | str | Идентификатор контекста диалога |
| `session` | SQLiteSession | Сессия памяти для агента |
| `user_id` | str | ID пользователя для изоляции workspace |
| `max_turns` | int | Максимальное количество ходов в сессии (по умолчанию из config) |
| `max_turns` | int | Максимальное количество ходов (по умолчанию из config) |
| `agent_id` | str | ID агента для изоляции |
| `metadata` | dict | Дополнительные метаданные из ContextManager |
| `container_id` | str | ID Docker-контейнера для изоляции |
    `session` | SQLiteSession | Сессия памяти для агента    `user_id` | str | ID пользователя для изоляции workspace    `max_turns` | int | Максимальное количество ходов (по умолчанию из config)    `agent_id` | str | ID агента для изоляции
### AgentFactory

Основной класс фабрики агентов.

#### Конструктор

```python
def __init__(
    self,
    config: Optional[Config] = None,
    working_directory: Optional[str] = None,
    *,
    tracing_level: Optional[str] = "INFO",
    stream_observer: Optional[StreamObserver] = None,
    broadcaster: Optional[Any] = None,
    unified_memory: Optional[Any] = None,
    memory_store: Optional[Any] = None,
    container_id: Optional[str] = None,
    enable_mcp: Optional[bool] = True,
):
```

**Параметры:**
| Параметр | Тип | Описание |
|----------|-----|----------|
| `config` | Config | Конфигурация (создаётся默认 если None) |
| `working_directory` | str | Путь к рабочей директории |
| `tracing_level` | str | Уровень трассировки ("INFO", "DEBUG", etc.) |
| `stream_observer` | StreamObserver | Наблюдатель потоковых событий |
| `broadcaster` | LiveTransparencyBroadcaster | Для прогресс-уведомлений в Telegram |
| `memory_store` | MemoryStore | SQLite-память (новый подход) |
| `container_id` | str | ID Docker-контейнера для изоляции |

**Инициализируемые компоненты:**
1. **Config** — загрузка конфигурации из `config.yaml`
2. **ContextManager** — управление историей диалога
3. **MemoryStore** — SQLite-база для long-term/short-term памяти
4. **SkillManager** — управление навыками (skills)
5. **ContainerManager** — управление Docker-изоляцией
6. **Кэши:**
   - `_agent_cache: Dict[str, Agent]` — кэш агентов
   - `_tool_cache: Dict[str, List[Any]]` — кэш инструментов
   - `_mcp_servers: Dict[str, Any]` — кэш MCP-серверов
   - `_agent_sessions: Dict[tuple, SQLiteSession]` — сессии на пару (агент, контекст)

---

## Методы AgentFactory

### create_agent

Создание или получение кэшированного агента из конфигурации.

```python
async def create_agent(
    self, 
    agent_key: str, 
    context_path: Optional[str] = None,
    force_reload: bool = False
) -> Agent:
```

**Параметры:**
| Параметр | Тип | Описание |
|----------|-----|----------|
| `agent_key` | str | Ключ агента из `config.yaml` |
| `context_path` | str | Путь к файлу контекста (опционально) |
| `force_reload` | bool | Принудительное пересоздание (минуя кэш) |

**Возвращает:** `Agent` — экземпляр агента OpenAI Agents SDK.

**Процесс создания:**
1. Проверка кэша (если не `force_reload`)
2. Загрузка конфига агента и модели
3. Валидация API-ключа провайдера
4. Создание OpenAI-клиента (с поддержкой proxy)
5. Выбор модели:
   - `OpenAIResponsesModel` — для reasoning-моделей с Responses API
   - `VisionChatCompletionsModel` — стандартный Chat Completions
6. Сбор инструментов (function + agent tools)
7. Создание MCP-серверов (если указаны в конфиге)
8. Построение инструкций с контекстом
9. Создание `Agent` SDK
10. Сохранение в кэш

**Пример:**
```python
factory = AgentFactory()
agent = await factory.create_agent("chat_agent")
```

---

### create_dynamic_agent

Создание динамического агента "на лету" (без записи в `config.yaml`).

```python
async def create_dynamic_agent(
    self,
    *,
    name: str,
    instructions: str,
    model_key: Optional[str] = None,
    tool_names: Optional[List[str]] = None,
    mcp_tool_names: Optional[List[str]] = None,
) -> Agent:
```

**Параметры:**
| Параметр | Тип | Описание |
|----------|-----|----------|
| `name` | str | Имя агента |
| `instructions` | str | Инструкции (prompt) агента |
| `model_key` | str | Ключ модели (из `config.yaml`) |
| `tool_names` | List[str] | Список function/agent инструментов |
| `mcp_tool_names` | List[str] | Список MCP-серверов |

**Возвращает:** `Agent` — экземпляр динамического агента.

**Важно:** 
- Проверяет whitelist разрешённых моделей (`settings.allowed_models`)
- Автоматически добавляет `prompt_addition` из конфигурации инструментов
- Поддерживает MCP-серверы

**Пример:**
```python
agent = await factory.create_dynamic_agent(
    name="Research Assistant",
    instructions="Ты исследователь. Найди информацию по теме...",
    model_key="kimi-k2.5",
    tool_names=["filesystem", "terminal"],
    mcp_tool_names=["git"],
)
```

---

### run_agent

Запуск агента с сообщением и управлением контекстом.

```python
async def run_agent(
    self,
    agent_key: str,
    message: str,
    context_path: Optional[str] = None,
    context_id: Optional[str] = None,
    *,
    stream: bool = False,

    use_active_context: bool = False,
    skip_input_add: bool = False,
    user_id: Optional[str] = None,
) -> str:
```

**Параметры:**
| Параметр | Тип | Описание |
|----------|-----|----------|
| `agent_key` | str | Ключ агента из конфига |
| `message` | str | Входное сообщение (может быть JSON для multimodal) |
| `context_path` | str | Путь к контексту |
| `context_id` | str | ID существующего контекста для продолжения диалога |
| `stream` | bool | Потоковая передача ответа |
| `use_active_context` | bool | Использовать активный контекст (не создавать новый) |
| `skip_input_add` | bool | Не добавлять сообщение в историю (для рекурсивных вызовов) |
| `user_id` | str | ID пользователя для изоляции workspace |

**Возвращает:** `str` — ответ агента.

**Особенности:**
1. **Управление контекстом:**
   - Создаёт новый контекст или использует существующий по `context_id`
   - Сохраняет историю диалога в `ContextManager`
   - Поддерживает multimodal-сообщения (изображения)

2. **Auto-run инструменты:**
   - Запускает инструменты из `agent_config.auto_run_tools` один раз на сессию
   - Например: `beads_init`, `beads_ready` для инициализации

3. **Мультимодальная поддержка:**
   - Парсит JSON-сообщения с изображениями
   - Конвертирует в формат SDK: `[{"type": "input_text", ...}, {"type": "input_image", ...}]`
   - Сохраняет изображения в контексте через `ContextMessage`

4. **Сессии памяти:**
   - Создаёт `SQLiteSession` на пару (агент, контекст)
   - Позволяет агенту помнить локальную историю в рамках сессии

**Пример:**
```python
# Новый диалог
response = await factory.run_agent("chat_agent", "Привет!")

# Продолжение диалога по context_id
response = await factory.run_agent(
    "chat_agent",
    "Продолжи предыдущую тему",
    context_id="ctx-abc123",
    use_active_context=True
)

# Multimodal сообщение с изображением
message = json.dumps({
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Что на этом изображении?"},
        {"type": "input_image", "image_url": "data:image/jpeg;base64,..."}
    ]
})
response = await factory.run_agent("vision_agent", message)
```

---

### run_agent_object_simple

Запуск экземпляра `Agent` напрямую (для динамических агентов).

```python
async def run_agent_object_simple(
    self,
    agent: Agent,
    message: str,
    *,
    context_id: Optional[str] = None,
    max_turns: Optional[int] = None,
    session: Optional[SQLiteSession] = None,
) -> str:
```

**Параметры:**
| Параметр | Тип | Описание |
|----------|-----|----------|
| `agent` | Agent | Экземпляр агента (например, из `create_dynamic_agent`) |
| `message` | str | Входное сообщение |
| `context_id` | str | ID контекста |
| `max_turns` | int | Максимальное количество ходов (по умолчанию из конфига) |
| `session` | SQLiteSession | Сессия памяти |

**Возвращает:** `str` — ответ агента.

**Особенности:**
- Не сохраняет полный диалог (лёгковесный режим)
- Передаёт `GridRunContext` для доступа инструментов к фабрике
- Логирует вызовы инструментов и вывод через `DYNAMIC_AGENT_*` события
- Обрабатывает ошибки:
  - `MaxTurnsExceeded` — возвращает частичный вывод
  - `ModelBehaviorError` — возвращает строку ошибки для повторной попытки
  - `AgentsUserError` — ошибка инструмента

**Пример:**
```python
agent = await factory.create_dynamic_agent(
    name="Helper",
    instructions="Ты помощник...",
    tool_names=["memory_tools"]
)
response = await factory.run_agent_object_simple(agent, "Запомни: meeting at 5pm")
```

---

### get_openai_client_for_model

Создание OpenAI-клиента для модели.

```python
def get_openai_client_for_model(self, model_key: str) -> tuple[AsyncOpenAI, str]:
```

**Параметры:**
- `model_key` — ключ модели из `config.yaml`

**Возвращает:** кортеж `(AsyncOpenAI client, model_name)`

**Пример:**
```python
client, model_name = factory.get_openai_client_for_model("kimi-k2.5")
```

---

### resolve_model_key

Резолюция входного ключа в ключ модели.

```python
def resolve_model_key(self, key: Optional[str]) -> str:
```

**Логика:**
1. Если `key` — None: использует модель агента по умолчанию
2. Если `key` — ключ модели: возвращает его
3. Если `key` — ключ агента: возвращает модель этого агента
4. Иначе: fallback на модель агента по умолчанию

**Пример:**
```python
model_key = factory.resolve_model_key("chat_agent")  # → "kimi-k2.5"
model_key = factory.resolve_model_key("kimi-k2.5")   # → "kimi-k2.5"
model_key = factory.resolve_model_key(None)          # → модель default_agent
```

---

### emit_progress

Отправка прогресс-ивентов в Telegram через `LiveTransparencyBroadcaster`.

```python
async def emit_progress(
    self,
    event_type: str,
    agent_name: str,
    content: str,
    parent_id: Optional[str] = None,
    status: str = "running",
    details: Dict[str, Any] = None
) -> None:
```

**Параметры:**
| Параметр | Тип | Описание |
|----------|-----|----------|
| `event_type` | str | Тип события (`agent_start`, `agent_end`, `tool_call`, ...) |
| `agent_name` | str | Имя агента |
| `content` | str | Описание события |
| `parent_id` | str | ID родительского агента (для дерева) |
| `status` | str | Статус (`running`, `completed`, `failed`) |
| `details` | Dict[str, Any] | Дополнительные детали для спойлера |

**Пример:**
```python
await factory.emit_progress(
    event_type="tool_call",
    agent_name="file_agent",
    content="Чтение файла config.yaml",
    status="running",
    details={"file": "config.yaml", "size": 60321}
)
```

---

### _build_model_settings

Построение `ModelSettings` с учётом reasoning-параметров.

```python
def _build_model_settings(self, model_config: Any) -> ModelSettings:
```

**Поддерживаемые форматы в конфиге:**

```yaml
models:
  o3-mini:
    provider: openai
    reasoning:
      effort: "none"        # SDK-native: reasoning_effort=<effort>
  
  deepseek-r1:
    provider: openrouter
    reasoning:
      enabled: false        # extra_body: {"reasoning": {"enabled": false}}
```

**Возвращает:** `ModelSettings(reasoning=..., extra_body=...)`

---

## Примеры использования

### 1. Базовое использование

```python
from core.agent_factory import AgentFactory

# Инициализация фабрики
factory = AgentFactory(
    tracing_level="INFO",
    stream_observer=ConsoleStreamObserver()
)

# Создание агента из конфига
agent = await factory.create_agent("chat_agent")

# Запуск агента
response = await factory.run_agent("chat_agent", "Привет! Как дела?")
print(response)
```

### 2. Динамический агент с инструментами

```python
# Создание динамического агента для исследования
researcher = await factory.create_dynamic_agent(
    name="Researcher",
    instructions="""
Ты исследователь. Твоя задача:
1. Найти информацию по запросу пользователя
2. Проанализировать результаты
3. Сделать выводы

Используй доступные инструменты для поиска и анализа.
""",
    model_key="kimi-k2.5",
    tool_names=["filesystem", "terminal", "memory_tools"],
    mcp_tool_names=["git"]
)

# Запуск
result = await factory.run_agent_object_simple(
    researcher,
    "Найди все Python-файлы в директории core/ и оцени их сложность"
)
```

### 3. Продолжение диалога

```python
# Первый запрос (создаёт новый контекст)
response1 = await factory.run_agent("chat_agent", "Расскажи про архитектуру Grid")
# context_id сохраняется в ContextManager

# Продолжение диалога
response2 = await factory.run_agent(
    "chat_agent",
    "А как работает AgentFactory?",
    use_active_context=True
)
```

### 4. Мультимодальный запрос с изображением

```python
import base64
import json

# Чтение изображения
with open("image.jpg", "rb") as f:
    image_data = base64.b64encode(f.read()).decode()

# Формирование multimodal-сообщения
message = json.dumps({
    "role": "user",
    "content": [
        {"type": "input_text", "text": "Что изображено на этой схеме?"},
        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_data}"}
    ]
})

# Запуск vision-агента
response = await factory.run_agent("vision_agent", message)
```

### 5. Прогресс-уведомления в Telegram

```python
from channels.live_transparency import LiveTransparencyBroadcaster

# Создание broadcaster
broadcaster = LiveTransparencyBroadcaster(channel="telegram_progress")

# Инициализация фабрики с broadcaster
factory = AgentFactory(broadcaster=broadcaster)

# В коде агента или инструмента:
await factory.emit_progress(
    event_type="agent_start",
    agent_name="file_agent",
    content="Начинаю анализ файлов...",
    status="running"
)

# После завершения
await factory.emit_progress(
    event_type="agent_end",
    agent_name="file_agent",
    content="Анализ завершён",
    status="completed"
)
```

### 6. Работа с сессиями памяти

```python
# Создание сессии для пары (агент, контекст)
session = factory._get_agent_session("chat_agent", "ctx-abc123")

# Запуск агента с сессией
response = await factory.run_agent_object_simple(
    agent,
    "Запомни: встреча в 17:00",
    session=session
)

# Следующий запрос в той же сессии
response2 = await factory.run_agent_object_simple(
    agent,
    "Напомни, о чём я просил ранее?",
    session=session
)
# Агент вспомнит предыдущее сообщение из сессии
```

---

## Интеграции

### MCP (Model Context Protocol)

AgentFactory поддерживает MCP-серверы через `agents.mcp.MCPServerStdio`.

**Настройка в `config.yaml`:**
```yaml
tools:
  terminal:
    type: mcp
    server_command: "npx @dillip285/mcp-terminal"
  filesystem:
    type: mcp
    server_command: "npx @modelcontextprotocol/server-filesystem"

agents:
  chat_agent:
    tools: [terminal, filesystem]
    mcp_enabled: true
```

**В коде:**
```python
agent = await factory.create_agent("chat_agent")
# MCP-серверы создаются автоматически и передаются в Agent(mcp_servers=...)
```

### Telegram

Интеграция через `LiveTransparencyBroadcaster` для отправки прогресс-ивентов.

**Сценарии использования:**
- Отображение статуса выполнения агента в Telegram
- Показ вызовов инструментов в реальном времени
- Дерево вызовов агентов (через `parent_id`)

### Docker Isolation

Поддержка изоляции агентов в Docker-контейнерах.

```python
factory = AgentFactory(
    container_id="abc123",
    working_directory="/workspace"
)
```

**Особенности:**
- `GridRunContext.container_id` передаётся в инструменты
- `ContainerManager` управляет маппингом путей
- Рабочая директория внутри контейнера: `/home/user/grid`

### Memory Store

SQLite-база для long-term и short-term памяти.

```python
from core.memory_store import MemoryStore

memory_store = MemoryStore(db_path="data/memory.db")
factory = AgentFactory(memory_store=memory_store)

# Пре-загрузка памяти для агента
preload = memory_store.get_preload_context(
    max_long_term=15,
    max_short_term=5,
    max_tasks=3
)
memory_text = memory_store.format_preload(preload, max_chars=2000)
```

---

## Отладка и логирование

### Уровни логирования

- `INFO` — основные события (запуск агента, вызовы инструментов)
- `DEBUG` — детальная информация
- `VERBOSE` — полные промпты, выводы инструментов

### Логи

| Лог | Описание |
|-----|----------|
| `DYNAMIC_AGENT_INPUT` | Входные данные динамического агента |
| `DYNAMIC_AGENT_OUTPUT` | Выходные данные динамического агента |
| `DYNAMIC_AGENT_TOOL_CALL` | Вызов инструмента |
| `DYNAMIC_AGENT_TOOL_OUTPUT` | Вывод инструмента |
| `DYNAMIC_AGENT_ERROR` | Ошибка выполнения |
| `MODEL_VALIDATION_PASSED/FAILED` | Валидация модели по whitelist |

### Трассировка

```python
factory = AgentFactory(tracing_level="INFO")
# Включает консольную трассировку через tracing_config.configure_console_tracing()
```

---

## Производительность

### Кэширование

- **Агенты:** `_agent_cache` — повторное использование созданных агентов
- **Инструменты:** `_tool_cache` — кэш function/agent инструментов
- **MCP-серверы:** `_mcp_servers` — кэш запущенных MCP-серверов
- **Сессии:** `_agent_sessions` — сессии на пару (агент, контекст)

### Singleton трассировки

```python
_TRACING_CONFIGURED = False
_TRACING_CONFIG_LOCK = threading.Lock()

@staticmethod
def _configure_tracing_once(level: str) -> None:
    global _TRACING_CONFIGURED
    if _TRACING_CONFIGURED:
        return
    with _TRACING_CONFIG_LOCK:
        if _TRACING_CONFIGURED:
            return
        tracing_config.configure_console_tracing(level)
        tracing_config.apply()
        _TRACING_CONFIGURED = True
```

---

## Обработка ошибок

| Исключение | Обработка |
|------------|-----------|
| `MaxTurnsExceeded` | Возврат частичного вывода, логирование `DYNAMIC_AGENT_MAX_TURNS` |
| `ModelBehaviorError` | Возврат строки ошибки для повторной попытки |
| `AgentsUserError` | Возврат строки ошибки инструмента |
| `AgentError` | Выбрасывается при ошибке создания/запуска агента |
| `ConfigError` | Выбрасывается при невалидной конфигурации |

---

## Устаревшие компоненты

### UnifiedMemory (deprecated)

**Статус:** Deprecated. Будет удалён в следующей версии. Используйте `memory_store` вместо этого.

**Описание:** Гибридное управление памятью (long-term/short-term).

**Ранее использовалось в:**
- Конструкторе `AgentFactory(unified_memory=UnifiedMemory(...))`

**Миграция:**
Перейдите на `memory_store: MemoryStore` (SQLite-based).

```
# Старое
factory = AgentFactory(unified_memory=um)

# Новое
factory = AgentFactory(memory_store=memory_store)
```

*Документация подготовлена на основе анализа `core/agent_factory.py` (2572 строки).*
