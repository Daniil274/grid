# Security Context: проект grid

> **Дата анализа:** 2026-04-26  
> **Версия проекта:** 0.1.0 (core/__init__.py)  
> **Анализируемый каталог:** `/` (корень проекта)  
> **Исключено:** .venv, .git, .beads, __pycache__, node_modules

---

## 1. СТРУКТУРА ПРОЕКТА

```
.
├── grid.py                          # CLI entrypoint (argparse)
├── agent_chat.py                    # Legacy chat interface (831 строк)
├── Dockerfile                       # ARM64 container build
├── requirements.txt                 # 42 строки зависимостей
├── pyproject.toml                   # Hatchling-based build
├── config.yaml                      # ** Рабочий конфиг (817 строк) **
├── config.yaml.example              # Пример конфига (464 строки)
├── .env                             # ** РЕАЛЬНЫЕ API-КЛЮЧИ **
├── .env.example                     # Шаблон переменных окружения
├── README.md                        # Документация (279 строк)
├── pytest.ini
│
├── core/                            # Ядро системы
│   ├── __init__.py                  # Реэкспорты, легаси-алиасы
│   ├── agent_factory.py             # (3635 строк!) Фабрика агентов
│   ├── context.py                   # ContextManager (947 строк)
│   ├── meta_cognitive.py
│   ├── skills_integration.py
│   ├── speech_processor.py          # Silero TTS + Whisper STT
│   ├── vision_model.py
│   ├── config/
│   │   ├── config.py               # Config (601 строка) + yaml.safe_load
│   │   ├── prompt_sections.py
│   │   └── protocols.py            # IConfig, IContextManager, IToolManager
│   ├── managers/
│   │   ├── session_manager.py      # SQLiteSession изоляция (150 строк)
│   │   ├── mcp_manager.py          # MCP server lifecycle (557 строк)
│   │   ├── model_manager.py        # Управление моделями
│   │   ├── tool_manager.py         # Инструменты
│   │   ├── instructions_builder.py # Сборка промптов
│   │   ├── skill_manager.py        # Управление скиллами
│   │   ├── container_manager.py    # Docker-контейнеры
│   │   └── project_tools_loader.py # Загрузка внешних инструментов
│   ├── memory/
│   │   ├── store.py                # SQLite MemoryStore (1810 строк)
│   │   ├── optimizer.py            # Оптимизация памяти
│   │   ├── unified.py              # Унифицированный интерфейс
│   │   └── embeddings.py           # OpenRouter эмбеддинги
│   ├── cognition/
│   │   ├── knowledge.py            # Онтология задач
│   │   ├── patterns.py             # Паттерны/шаблоны
│   │   └── health.py               # Анализ жизненного цикла
│   ├── platform/                   # Системная платформа
│   │   ├── runtime.py              # SystemRuntime (307 строк)
│   │   ├── compiler.py             # Компилятор графов
│   │   ├── registry.py             # Реестр систем
│   │   ├── governance.py           # PermissionChecker, BudgetTracker
│   │   ├── events.py               # DomainEvent шина
│   │   ├── conditions.py           # ConditionEvaluator
│   │   ├── mutation.py             # SystemMutator
│   │   ├── builder.py              # LiveSystemBuilder
│   │   ├── release.py              # LifecycleStateMachine
│   │   └── workbench.py            # SystemWorkbench
│   ├── compact/                    # Сжатие контекста
│   │   ├── auto_compact.py
│   │   ├── compact_conversation.py
│   │   ├── session_memory_compact.py
│   │   ├── micro_compact.py
│   │   ├── reactive_compact.py
│   │   ├── post_compact.py
│   │   ├── grouping.py
│   │   ├── utils.py
│   │   ├── prompts.py
│   │   └── base.py
│   ├── tracing/                    # Трассировка
│   │   ├── config.py
│   │   ├── tracer.py               # ExecutionTracer (SQLite)
│   │   ├── pipeline_registry.py
│   │   └── events.py
│   └── improvement/                # Самоулучшение
│       ├── registry.py
│       ├── monitor.py
│       ├── proposer.py
│       ├── evaluator.py
│       ├── log_observer.py
│       └── config_apply.py
│
├── tools/                          # 23 файла инструментов
│   ├── function_tools.py           # Реестр всех инструментов
│   ├── git_tools.py, file_tools.py, beads_tools.py
│   ├── system_tools.py, orchestrator_tools.py
│   ├── memory_tools_v2.py, semantic_memory_tools.py
│   ├── vision_tools.py, ocr_tools.py, screen_tools.py
│   ├── history_tools.py, skill_tools.py, emergency_tools.py
│   └── ...
│
├── utils/                          # Утилиты (7 файлов)
│   ├── path_utils.py               # ** Защита от path traversal **
│   ├── image_utils.py              # Валидация изображений
│   ├── multimodal_converter.py
│   ├── cli_chat.py, logger.py, exceptions.py
│
├── schemas/                        # Pydantic модели
│   ├── schemas.py                  # ProviderConfig, ModelConfig, ToolConfig и др.
│   ├── system_platform.py, system_builder.py
│   └── ...
│
├── examples/                       # Примеры конфигураций
│   ├── claude-tools/               # Claude Code-совместимые тулы
│   │   ├── tools/bash_tool.py      # ** subprocess shell=True **
│   │   ├── tools/shell.py          # ** PowerShell/CMD exec **
│   │   ├── mcp_server.py           # ** exec() вызов **
│   │   └── ...
│   ├── windows-computer-use/       # Windows automation
│   │   ├── tools/shell.py          # ** shell execution **
│   │   └── tools/_ps_uia.py        # ** PowerShell subprocess **
│   ├── coordinator-pipeline/
│   └── project-assistant/
│
├── tests/                          # Тесты
├── benchmarks/                     # Бенчмарки
├── scripts/
│   └── clean_config.py             # ** yaml.load (небезопасно) **
├── timeline/                       # Дашборд
├── data/                           # Данные времени выполнения
│   ├── memory.db                   # SQLite база памяти
│   ├── timeline.db                 # SQLite база трассировки
│   ├── todos.json                  # TODO список
│   └── user_790241050/context.json # Контекст пользователя
└── logs/                           # Логи сессий
```

### Сводка размеров

| Компонент           | Файлов | Ключевые метрики                              |
|----------------------|--------|-----------------------------------------------|
| core/                | ~40    | agent_factory.py = 3635 строк (монолит)       |
| tools/               | 23     | git, file, memory, vision, beads, screen и др.|
| examples/            | 48     | Включая вложенные workspace, data, skills     |
| schemas/             | 9      | Pydantic-модели                               |
| utils/               | 7      | path_utils — критичный для безопасности       |

---

## 2. КРИТИЧЕСКИЕ ФАЙЛЫ

### 2.1. Файлы с реальными секретами (КРИТИЧЕСКИ!)

| Файл       | Секрет                          | Строка | Риск                          |
|------------|---------------------------------|--------|-------------------------------|
| `.env`     | `OPENROUTER_API_KEY=sk-or-v1-...` | 5      | **Реальный API-ключ OpenRouter** |
| `.env`     | `TELEGRAM_BOT_TOKEN=8138042180:...` | 12     | **Реальный токен Telegram бота** |
| `.env`     | `OPENCODE_API_KEY=sk-qiMwZ...`     | 14     | **Реальный API-ключ OpenCode** |
| `config.yaml` | `telegram.token_env: TELEGRAM_BOT_TOKEN` | 44 | Косвенная утечка (ссылается на .env) |

> ⚠️ **Файл `.env` НЕ должен быть закоммичен!** Он содержит три реальных API-ключа.

### 2.2. Конфигурационные файлы

| Файл                | Строк | Назначение                                      |
|---------------------|-------|--------------------------------------------------|
| `config.yaml`       | 817   | **Рабочий конфиг** (агенты, модели, MCP, tools)  |
| `config.yaml.example` | 464 | Пример конфига (содержит Windows-пути)           |
| `.env.example`      | 26    | Шаблон переменных окружения                      |
| `pyproject.toml`    | 98    | Зависимости, скрипты, настройки линтеров         |
| `Dockerfile`        | 82    | ARM64 контейнер, non-root user (useradd agent)   |

### 2.3. Файлы исполнения кода

| Файл                                    | Строка | Тип                       | Контекст                           |
|-----------------------------------------|--------|---------------------------|------------------------------------|
| `examples/claude-tools/mcp_server.py`   | 164    | `exec(fn_src, globs)`     | Динамическая компиляция function tool |
| `core/speech_processor.py`              | 151    | `.load_pickle(...)`       | Silero TTS модель через torch.package |
| `scripts/clean_config.py`               | 9      | `yaml.load(f)`            | ruamel.yaml (НЕ safe_load!)         |
| `examples/claude-tools/tools/bash_tool.py` | 121-122 | `shell=True`           | subprocess с shell-интерпретацией   |

---

## 3. ПОТЕНЦИАЛЬНО ОПАСНЫЕ ПАТТЕРНЫ (grep)

### 3.1. `exec|eval|subprocess|os.system|shell` (15 matches)

| Файл | Строка | Код | Уровень риска |
|------|--------|-----|---------------|
| `examples/claude-tools/mcp_server.py` | 164 | `exec(fn_src, globs)  # noqa: S102` | **CRITICAL** — динамическое исполнение кода |
| `examples/claude-tools/tools/bash_tool.py` | 121-122 | `subprocess.run(shell_cmd, shell=True, ...)` | **CRITICAL** — shell injection risk |
| `examples/windows-computer-use/tools/shell.py` | 23-25 | `subprocess.run(args, ...)` — Windows shell exec | **HIGH** — raw shell execution |
| `examples/windows-computer-use/tools/_ps_uia.py` | 91-92 | `subprocess.run(["powershell", ...])` | **HIGH** — PowerShell subprocess |
| `core/agent_factory.py` | 3594-3595 | `subprocess.run(["docker", "exec", self.container_id, "pkill", ...])` | **MEDIUM** — container escape vector |
| `core/speech_processor.py` | 114-115 | `subprocess.run(["ffplay", "-nodisp", "-autoexit", ...])` | LOW — local audio playback |
| `core/speech_processor.py` | 435-436 | `subprocess.run(["ffmpeg", "-version"], ...)` | LOW — version check only |
| `tools/git_tools.py` | 125, 133 | `subprocess.run(["git", ...], cwd=..., ...)` | MEDIUM — git commands (cwd from agent) |
| `tools/beads_tools.py` | 121, 198, 206 | `subprocess.run(["bd", ...], cwd=..., ...)` | MEDIUM — bd commands |
| `tools/ocr_tools.py` | 108, 148, 192 | `subprocess.run([tesseract...])` | LOW — OCR processing |
| `examples/claude-tools/tools/search_tools.py` | 209, 353 | `subprocess.run([rg/grep...])` | MEDIUM — search commands |

**Основные выводы:**
- **`exec()` используется 1 раз** — в mcp_server.py для динамической компиляции function tool'ов из исходного кода. Хотя стоит `# noqa: S102`, это всё равно вектор code injection.
- **`shell=True` используется 1 раз** — в bash_tool.py. Путь к рабочей директории очищается от двойных кавычек (`safe_path = str(work_path).replace('"', "")`), но команда (`command`) передаётся как есть — классический shell injection.
- **`subprocess.run` без shell=True** — в большинстве случаев используется безопасная форма передачи аргументов списком.

### 3.2. `secret|password|api_key|token|credential` (100+ matches)

Ключевые находки (security-relevant):

| Файл | Строка | Контекст |
|------|--------|----------|
| `core/config/config.py` | 314-324 | `get_api_key()` — возвращает `provider.api_key` или `os.getenv(provider.api_key_env)` |
| `core/managers/model_manager.py` | 96-100 | Проверка наличия API-ключа, передача в OpenAI клиент |
| `core/agent_factory.py` | 790-798, 907-913 | Создание AsyncOpenAI клиента с api_key |
| `core/memory/embeddings.py` | 65-85 | EmbeddingsManager требует api_key |
| `core/memory/store.py` | 144, 162 | Проверка OPENROUTER_API_KEY для семантического поиска |
| `schemas/schemas.py` | 22-23 | `ProviderConfig.api_key_env` и `ProviderConfig.api_key` поля |
| `config.yaml` | 44 | `telegram.token_env: TELEGRAM_BOT_TOKEN` |
| `config.yaml.example` | 54, 61 | `api_key: "lm-studio"`, `api_key_env: "OPENROUTER_API_KEY"` |

> 🟡 API-ключи передаются через переменные окружения (рекомендовано), но в `.env` лежат в открытом виде.

### 3.3. `sqlite|.execute()|.cursor()` (70 matches)

| Файл | Ключевые строки | Тип операций |
|------|-----------------|---------------|
| `core/memory/store.py` | 15, 132, 144, 493-498 | FTS5 поиск, CRUD операций памяти |
| `core/tracing/tracer.py` | 15, 95, 131, 168, 375-376 | Сохранение trace'ов в SQLite |
| `core/managers/skill_manager.py` | 323, 365 | Запросы навыков |
| `core/memory/optimizer.py` | 4, 449, 532 | Оптимизация памяти |
| `tools/history_tools.py` | 12, 100-101, 200-201, 221 | Чтение истории сессий |
| `core/agent_factory.py` | 32, 321, 365, 411, 415 | SQLiteSession для памяти агентов |

> 🟡 Все SQL-запросы используют параметризацию (плейсхолдеры `?`), но данные хранятся в открытом виде (без шифрования).

### 3.4. `hash|encrypt|decrypt|cipher` (14 matches)

| Файл | Строка | Контекст |
|------|--------|----------|
| `core/memory/embeddings.py` | 24, 163 | `hashlib.md5(raw.encode("utf-8")).hexdigest()` — хеширование для embeddings |
| `core/managers/project_tools_loader.py` | 14, 101, 118 | `hashlib.sha1(...)` — хеширование путей для кеша |
| `core/agent_factory.py` | 2913 | `hash(tuple(agent_config.tools))` — кеш-ключ |
| `tools/semantic_tools.py` | 11, 55 | `hashlib.md5(...)` — хеш пути для ChromaDB коллекции |
| `tools/git_tools.py` | 306-307, 1103-1117 | `commit_hash` для git reset (не криптографический контекст) |

> 🔴 **Шифрование НЕ используется.** Нет вызовов `encrypt`, `decrypt`, `cipher`.  
> 🔴 MD5 используется для хеширования (небезопасный алгоритм, но здесь не для криптографии).  
> 🔴 SHA1 также используется (небезопасный, но для кеша путей допустимо).

### 3.5. `pickle|marshal|yaml.load` (2 matches)

| Файл | Строка | Код | Риск |
|------|--------|-----|------|
| `core/speech_processor.py` | 151 | `torch.package.PackageImporter(str(model_path)).load_pickle("tts_models", "model")` | **HIGH** — десериализация pickle из файла модели |
| `scripts/clean_config.py` | 9 | `data = yaml.load(f)` (ruamel.yaml, НЕ safe) | **MEDIUM** — небезопасная загрузка YAML |

> 🔴 `pickle` десериализация в speech_processor.py может привести к RCE, если файл модели скомпрометирован.  
> 🟡 `yaml.load` в clean_config.py: хоть ruamel.yaml.YAML().load() по умолчанию безопаснее PyYAML, он всё же может создавать произвольные Python-объекты.

### 3.6. `sanitize|validate|escape` (100+ matches)

Защитные механизмы присутствуют:

| Файл | Строка | Механизм |
|------|--------|----------|
| `utils/path_utils.py` | 74 | `raise ValueError("Path escapes working directory")` — защита от path traversal |
| `utils/path_utils.py` | 31 | `_sanitize_path_component()` — очистка имён файлов |
| `core/managers/skill_manager.py` | 31-32 | `_sanitize_path_component()` — только alphanum + `-`, `_` |
| `utils/image_utils.py` | 300-353 | `validate_image_source()` — валидация источников изображений |
| `tools/emergency_tools.py` | 91 | Валидация severity |
| `tools/memory_tools_v2.py` | 167, 172, 249, 442, 447 | Валидация type, importance, action, entry_id |
| `schemas/schemas.py` | 257-268 | Pydantic field_validator для agent_models и agent_tools |
| `examples/claude-tools/tools/bash_tool.py` | 152 | Валидация timeout |

> 🟢 Защита от path traversal реализована на хорошем уровне в `utils/path_utils.py`, но её применение зависит от каждого конкретного инструмента (не все используют `resolve_agent_path`).

---

## 4. ТОЧКИ ВХОДА ДАННЫХ

### 4.1. CLI входы

| Вход | Файл | Строки | Описание |
|------|------|--------|----------|
| `grid.py main()` | grid.py | 14-80 | CLI команды: observe, approve, propose, evaluate, canary, monitor |
| `agent_chat.py main()` | agent_chat.py | 450+ | Интерактивный чат (stdin) |
| `agent_chat.py parse_message_with_images()` | agent_chat.py | 67-100 | Парсинг пользовательского ввода с изображениями |

### 4.2. API/сетевые входы

| Вход | Файл | Строки | Протокол |
|------|------|--------|----------|
| OpenAI API | core/agent_factory.py | 790-810 | HTTPS (через AsyncOpenAI) |
| MCP servers | core/managers/mcp_manager.py | 323-360 | stdio subprocess |
| Telegram Bot | config.yaml | 44 | Telegram API |
| FastAPI ingestor | core/tracing/config.py | — | HTTP tracing endpoint |
| Docker API | core/managers/container_manager.py | — | Docker socket |

### 4.3. Файловые входы

| Вход | Файл | Механизм |
|------|------|----------|
| config.yaml | core/config/config.py | `yaml.safe_load()` ✅ |
| .env | python-dotenv | `os.getenv()` через dotenv |
| MCP server commands | core/managers/mcp_manager.py | Из config.yaml (tool_config.server_command) |
| Project tools | core/managers/project_tools_loader.py | Загрузка .py файлов из `tools_directory` |
| Skills (.md) | core/managers/skill_manager.py | Чтение Markdown файлов навыков |
| Speech model | core/speech_processor.py | torch.package.PackageImporter (pickle!) |

### 4.4. Агентский ввод (LLM-generated)

Все инструменты получают входные параметры от LLM через function calling. Это означает, что **любой параметр инструмента может быть произвольной строкой от LLM** (потенциально злонамеренной).

---

## 5. ТЕХНОЛОГИИ

| Технология | Назначение | Версия |
|------------|------------|--------|
| **openai-agents** | AI Agent SDK | >=0.2.6 |
| **openai** | OpenAI API клиент | >=1.0.0 |
| **pydantic** | Валидация данных | >=2.0.0 |
| **PyYAML / ruamel.yaml** | Конфигурация | >=6.0 / >=0.18.10 |
| **httpx** | HTTP клиент | >=0.25.0 |
| **fastapi + uvicorn** | HTTP сервер (tracing) | >=0.100.0 |
| **SQLite / sqlite3** | Локальная БД (память, tracing, сессии) | Встроенный |
| **ChromaDB** | Векторная БД | >=0.4.0 |
| **docker** | Контейнеризация | >=7.0.0 |
| **pyautogui** | Управление мышью/клавиатурой | >=0.9.0 |
| **torch** | Silero TTS (speech) | — (импортируется) |
| **python-dotenv** | Переменные окружения | >=1.0.0 |
| **click, rich** | CLI | >=8.0.0, >=13.0.0 |
| **beads (bd)** | Dolt-based version control | Установлен в Dockerfile |
| **MCP (Model Context Protocol)** | Интеграция инструментов | servers: filesystem, sequential-thinking, terminal |

---

## 6. СЛЕДУЮЩИЕ ШАГИ (рекомендации по анализу безопасности)

### 🔴 Критические (немедленно)

1. **Немедленно убрать `.env` из репозитория**
   - Файл `.env` содержит реальные API-ключи: `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `OPENCODE_API_KEY`
   - Добавить `.env` в `.gitignore` (уже есть, но файл зачем-то закоммичен)
   - **Скомпрометированные ключи необходимо отозвать и перевыпустить!**

2. **Заменить `exec()` в `examples/claude-tools/mcp_server.py:164`**
   - Динамическое исполнение кода из строк — вектор code injection
   - Рекомендация: использовать `types.FunctionType` или предварительно скомпилированные модули

3. **Заменить `yaml.load()` в `scripts/clean_config.py:9`**
   - Использовать `yaml.safe_load()` или `YAML(typ='safe')` для ruamel.yaml

4. **Устранить `shell=True` в `examples/claude-tools/tools/bash_tool.py:121-122`**
   - Текущая очистка `safe_path.replace('"', '')` недостаточна
   - Перейти на передачу команды списком: `["cmd", "/c", command]` с `shell=False`

### 🟠 Высокий приоритет

5. **Аудит использования pickle в `core/speech_processor.py:151`**
   - `torch.package.PackageImporter.load_pickle` — потенциальный RCE вектор
   - Проверить來源 модели (откуда загружается model.pt)

6. **Проверка MCP server команд в config.yaml**
   - `server_command` берётся из конфига (может быть любая команда)
   - Проверить, что нет инъекций через конфигурационные пути

7. **Аудит прямых subprocess-вызовов**
   - `core/agent_factory.py:3594-3595` — `docker exec ... pkill` (container escape)
   - Все git/beads/ocr вызовы — проверить escaping аргументов

8. **Шифрование чувствительных данных в SQLite**
   - `data/memory.db`, `data/timeline.db` хранят данные в открытом виде
   - Рассмотреть SQLCipher или полевое шифрование для PII

### 🟡 Средний приоритет

9. **Усилить валидацию входных данных от LLM**
   - Все tool-параметры приходят от LLM — формально недоверенный источник
   - Добавить строгую валидацию для file_path, command, directory и т.д.

10. **Аудит container escape векторов**
    - `docker exec` в MCPManager и AgentFactory
    - Проверить `network_mode=host` (упомянут в agent_factory.py:3588)

11. **Безопасность Telegram интеграции**
    - Токен бота в `.env` скомпрометирован
    - Проверить обработку входящих сообщений от Telegram

12. **Замена MD5/SHA1 на SHA-256**
    - Для не-криптографических целей допустимо, но снижает общий уровень безопасности

### 🟢 Низкий приоритет

13. **Добавить линтер безопасности (bandit) в CI**
    - Многие находки (exec, shell=True, pickle) были бы пойманы автоматически

14. **Документировать модель угроз**
    - Trust boundaries: LLM ↔ Tools, Config ↔ Runtime, MCP ↔ Host
    - Определить, что является доверенным вводом, а что нет

15. **Аудит зависимостей**
    - `pyautogui` (управление мышью/клавиатурой)
    - `torch` (десериализация моделей)
    - `chromadb` (векторная БД)
    - Все MCP серверы (npm пакеты)

---

## Приложение A: Полный список файлов с subprocess

```
examples/claude-tools/tools/bash_tool.py        (shell=True!)
examples/claude-tools/tools/search_tools.py     (rg/grep)
examples/windows-computer-use/tools/shell.py    (powershell/cmd)
examples/windows-computer-use/tools/_ps_uia.py  (powershell)
core/agent_factory.py                            (docker exec)
core/speech_processor.py                         (ffplay, ffmpeg)
tools/git_tools.py                               (git commands)
tools/beads_tools.py                             (bd commands)
tools/ocr_tools.py                               (tesseract)
```

## Приложение B: Файлы SQLite баз

```
data/memory.db              — основная база памяти (MemoryStore)
data/timeline.db            — база трассировки (ExecutionTracer)
logs/agent_sessions.db      — сессии агентов (SQLiteSession)
examples/*/data/memory.db   — базы примеров
```

---

*Отчёт сгенерирован автоматически агентом сбора контекста безопасности.*
*Для углублённого анализа рекомендуется аудит каждого из перечисленных файлов вручную.*
