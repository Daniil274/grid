## Grid Agent System

Система оркестрации AI-агентов для инженерных задач с чёткой архитектурой, строгим логированием, безопасностью и OpenAI-совместимым API.

### Назначение
- **Оркестрация**: координация специализированных агентов (файлы, Git, анализ задач, безопасность и др.).
- **Инструменты**: единый слой инструментов (файловые, Git, MCP), проксируемых в модели.
- **API**: OpenAI-совместимые эндпоинты для интеграции с внешними клиентами и инструментами.
- **Наблюдаемость**: единый унифицированный логгер, трейсинг вызовов инструментов, сохранение сессий агентов.

## Возможности
- **Агенты**: конфигурируемые профили (модель, инструменты, промпт). Поддержка подагентов как инструментов (`call_*`).
- **Инструменты**: чтение/запись/поиск файлов, операции Git, MCP-интеграция.
- **Контекст**: передача контекста между агентами и инструментами, сессии памяти (`SQLiteSession`).
- **Безопасность**: Security-aware фабрика, middleware для аутентификации, ограничение скорости, базовые guardrails.
- **API**: Chat Completions/Completions (совместимое с OpenAI), агенты, системные маршруты.
- **Логирование**: унифицированные структурированные логи, журнал вызовов инструментов, сбор метрик.

## Архитектура
- `core/`
  - `config.py`: загрузка и валидация `config.yaml`, управление путями, провайдерами, моделями и агентами.
  - `agent_factory.py`: создание/кэширование агентов, сбор инструментов, запуск через `Runner.run`, унифицированное логирование, сессии.
  - `security_agent_factory.py`: расширение фабрики с security guardrails для заданных типов агентов.
  - `context.py`: управление контекстом диалога и выполнений.
- `tools/`
  - `file_tools.py`: файловые операции.
  - `git_tools.py`: обёртки над Git с валидацией и логированием.
  - `function_tools.py`: интегратор и реестр доступных инструментов, алиасы, статистика.
  - `mcp.py`: интеграция MCP (если включено).
- `api/`
  - `main.py`: FastAPI-приложение, middleware, обработчики ошибок, маршруты.
  - `routers/`: OpenAI-совместимые эндпоинты, CRUD агентов, системные эндпоинты.
- `utils/`: унифицированный логгер, метрики, форматтеры, исключения.
- `schemas.py`: Pydantic-схемы конфигурации и выполнения.

## Установка
1) Клонирование и окружение
```
git clone <repo-url>
cd agents
python -m venv .venv
# Windows PowerShell
. .venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate
```
2) Зависимости
```
pip install -r requirements.txt           # ядро и CLI
pip install -r requirements-api.txt       # зависимости API (FastAPI/uvicorn и др.)
```
3) Конфигурация
```
copy config.yaml.example config.yaml       # Windows
# или
cp config.yaml.example config.yaml         # Linux/macOS
```
Заполните `config.yaml` под себя (см. раздел «Конфигурация»).

4) Переменные окружения
Создайте `.env` (по аналогии с `.env.example`, если есть) и задайте API-ключи, либо используйте переменные окружения, соответствующие `providers.*.api_key_env`.

Примечание для Windows/pytest: добавьте текущую папку в `PYTHONPATH` на время сессии:
```
$env:PYTHONPATH = "."
```

## Запуск
### CLI (локальный агент)
- Режим чата:
```
python agent_chat.py
```
- Один запрос:
```
python agent_chat.py --message "List files in current directory"
```
- Явный агент:
```
python agent_chat.py --agent file_agent --message "Read config.yaml"
```

### API (FastAPI)
- Утилита запуска:
```
python start_api.py
```
- Либо напрямую через uvicorn:
```
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
```
Документация: `http://localhost:8000/docs`
Проверка здоровья: `http://localhost:8000/health`

## Конфигурация
Конфигурация задаётся в `config.yaml` и валидируется через Pydantic (`schemas.py`).

### Общие настройки (`settings`)
- `default_agent`: агент по умолчанию.
- `max_history`: размер истории контекста.
- `max_turns`: лимит ходов для агентов (распространяется и на подагенты-инструменты).
- `agent_timeout`: таймаут выполнения агента (сек).
- `working_directory`: рабочая директория процесса (меняется при старте конфигурации).
- `config_directory`: директория с конфигурацией.
- `allow_path_override`: разрешить менять рабочую директорию из кода.
- `mcp_enabled`: включить MPC-глобально.
- `agent_logging`: параметры логирования агента.

### Провайдеры (`providers`)
- Базовый URL, ключ API (через переменную окружения), таймауты и ретраи.

### Модели (`models`)
- Идентификатор модели, провайдер, температурa, лимиты токенов и др.
- `use_responses_api`: флаг использования Responses API для reasoning-моделей (задаётся в конфиге модели, не хардкодится в коде). При отсутствии поддержки у провайдера система автоматически и бесшумно (с дедупликацией предупреждений) переключится на Chat Completions [[memory:5609856]].

Пример модели:
```yaml
models:
  gpt-4:
    name: "gpt-4"
    provider: "openai"
    temperature: 0.7
    max_tokens: 4000
    use_responses_api: false
```

### Агенты (`agents`)
- Имя, модель, инструменты, базовый/кастомный промпт, описание.
- Инструменты типов: `function` (прямые инструменты), `agent` (вызов подагента через `call_*`), `mcp` (инструменты сервера MCP).

Пример агента:
```yaml
agents:
  file_agent:
    name: "File Agent"
    model: "gpt-4"
    tools: ["file_read", "file_write", "file_list"]
    base_prompt: "with_files"
    description: "Специалист по файловым операциям"
```

### Инструменты (`tools`)
- `function`: подключаются из `tools/file_tools.py`, `tools/git_tools.py` и реестра в `tools/function_tools.py`.
- `agent`: создают инструмент `call_<agent_key>` для вызова подагента с передачей контекста. Поддерживаются параметры контекст-шеринга (`context_strategy`, `context_depth`, `include_tool_history`).
- `mcp`: инструменты сторонних MCP-серверов (включаются при `mcp_enabled`).

Особенности инструментов-агентов:
- Принимают вход в поле `input`. Для совместимости поддерживаются алиасы `task`, `message`, `prompt` — будут автоматически нормализованы до `input`.
- Выполнение подагента наследует `max_turns` из `settings.max_turns` и использует собственную `SQLiteSession`.

## API (OpenAI-совместимое)
Основные маршруты (префикс `/v1`):
- `POST /chat/completions` — совместимо с OpenAI Chat Completions. Поддерживает `stream`.
- `POST /completions` — устаревший формат, конвертируется во внутренний Chat Completions и обратно.
- `GET /agents` и др. — список и детали агентов.
- `GET /system/health` — проверка здоровья.

Конвертер форматов и контекста расположен в `api/utils/openai_converter.py` (маршрутизация в `api/routers/openai_compatible.py`).

## Логирование и наблюдаемость
- Унифицированный логгер (`utils/unified_logger.py`) для запусков агентов, вызовов инструментов и результатов.
- Структурные логи, запись в `logs/`, хранение сессий агентов в `logs/agent_sessions.db`.
- Дедупликация предупреждений по Responses API (снижение шума в логах).
- При запуске API контекст очищается, а сохранённые файлы контекста удаляются.

## Безопасность
- Security-aware фабрика (`core/security_agent_factory.py`) применяет guardrails к указанным агентам.
- Middleware: аутентификация, безопасность запросов, ограничения скорости.
- Команды Git запускаются с валидацией параметров и таймаутами; файловые операции проверяют существование/тип путей.

## Тестирование
```
# В Windows перед запуском тестов в PowerShell:
$env:PYTHONPATH = "."
pytest -q
```
Покрытие:
```
pytest --cov=. --cov-report=html
```

## Устранение неполадок
- `ModuleNotFoundError: No module named 'api'` при `pytest` в Windows — задайте `PYTHONPATH`:
  - На сессию: `$env:PYTHONPATH = "."`
- `Max turns exceeded` — увеличьте `settings.max_turns` или упростите задачу.
- Ошибка схемы инструмента: используйте `input` (или `task`/`message`/`prompt`, которые нормализуются автоматически).
- Предупреждения про Responses API — провайдер не поддерживает; установите `use_responses_api: false` для модели или используйте совместимый провайдер.

## Лицензия
MIT. См. файл `LICENSE`.