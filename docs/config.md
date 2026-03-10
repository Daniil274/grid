# Конфигурация проекта

## Оглавление
1. [Структура config.yaml](#структура-configyaml)
2. [Раздел `settings`](#раздел-settings)
3. [Раздел `isolation`](#раздел-isolation)
4. [Раздел `telegram`](#раздел-telegram)
5. [Раздел `voice`](#раздел-voice)
6. [Раздел `providers`](#раздел-providers)
7. [Раздел `models`](#раздел-models)
8. [Раздел `checkers`](#раздел-checkers)
9. [Раздел `tools`](#раздел-tools)
10. [Переменные окружения](#переменные-окружения)
11. [Примеры конфигурации](#примеры-конфигурации)

---

## Структура `config.yaml`

Файл `config.yaml` состоит из нескольких основных секций:

| Секция | Описание |
|--------|----------|
| `settings` | Глобальные параметры системы. |
| `isolation` | Настройки изоляции агентов (Docker). |
| `telegram` | Параметры Telegram‑бота. |
| `voice` | Конфигурация голосового ввода/вывода. |
| `providers` | Описание провайдеров API (lm‑studio, openrouter, anthropic и др.). |
| `models` | Перечень доступных моделей с привязкой к провайдерам. |
| `checkers` | Конфигурация проверяющих (audit). |
| `tools` | Набор MCP‑инструментов и агентов‑инструментов. |

---

## Раздел `settings`

Глобальные настройки системы.

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `default_agent` | `string` | Идентификатор агента, запускающегося по умолчанию. | `"chat_agent"` |
| `max_history` | `int` | Максимальное количество сообщений, хранимых в истории. | `50` |
| `max_turns` | `int` | Максимальное количество ходов в сессии. | `100` |
| `agent_timeout` | `int` (сек) | Таймаут выполнения агента. | `600` |
| `debug` | `bool` | Флаг отладки. | `false` |
| `max_tool_output_tokens` | `int` | Ограничение на количество токенов, выдаваемых инструментами (0 – без ограничения). | `12000` |
| `mcp_enabled` | `bool` | Включение MCP‑подсистемы. | `true` |
| `working_directory` | `string` | Путь к рабочей директории проекта. | `"./"` |
| `config_directory` | `string` | Путь к директории с конфигурационными файлами. | `"./"` |
| `logs_directory` | `string` | Путь к директории, где сохраняются логи. | `"./logs"` |
| `allow_path_override` | `bool` | Разрешить переопределять путь к рабочей директории при запуске. | `true` |
| `agent_logging` | section | Параметры логирования агентов | |
| `image_processing` | section | Параметры обработки изображений | |

### Параметры `agent_logging`

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Включить логирование. | `true` |
| `level` | `string` | Уровень логирования (`full`, `minimal`, …). | `"full"` |
| `save_prompts` | `bool` | Сохранять подсказки. | `true` |
| `save_conversations` | `bool` | Сохранять диалоги. | `true` |
| `save_executions` | `bool` | Сохранять результаты выполнения. | `true` |

### Параметры `image_processing`

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Включить обработку изображений. | `true` |
| `auto_resize` | `bool` | Автоматически изменять размер изображений. | `true` |
| `max_width` | `int` | Максимальная ширина (px). | `1920` |
| `max_height` | `int` | Максимальная высота (px). | `1080` |
| `max_file_size_mb` | `int` | Максимальный размер файла (МБ). | `10` |
| `jpeg_quality` | `int` | Качество JPEG (0‑100). | `85` |

---

## Раздел `isolation`

Настройки изоляции агентов.

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Включить изоляцию агентов. | `true` |
| `type` | `string` | Тип изоляции (`docker`, `process`, …). | `"docker"` |
| `image` | `string` | Docker‑образ, используемый для изоляции. | `"grid-agent:latest"` |

---

## Раздел `telegram`

Конфигурация Telegram-бота.

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `token_env` | `string` | Имя переменной окружения, содержащей токен бота. | `"TELEGRAM_BOT_TOKEN"` |
| `polling_timeout` | `int` | Таймаут для polling‑запроса (сек). | `30` |
| `workspace_path` | `string` | Путь к директории workspace внутри проекта. | `"./workspace"` |
| `persist_path` | `string` | Путь к директории, где сохраняются данные бота. | `"./data"` |
| `max_message_history` | `int` | Количество последних сообщений, сохраняемых в памяти. | `15` |
| `memory_commands_enabled` | `bool` | Включить команды памяти. | `true` |
| `enable_transparency` | `bool` | Показать прозрачность (отображение внутренних шагов). | `false` |
| `show_tool_calls` | `bool` | Показать вызовы инструментов в сообщениях. | `false` |
| `allowed_users` | `list|null` | Список Telegram‑ID, которым разрешён доступ. `null` – все пользователи. | `null` |
| `max_concurrent_tasks_per_user` | `int` | Максимальное количество одновременно выполняемых задач на пользователя. | `1` |
| `progress_update_interval` | `float` | Интервал обновления прогресса (сек). | `2.0` |

---

## Раздел `voice`

Конфигурация голосового ввода/вывода (STT/TTS). В разделе `voice` также описаны вложенные секции `stt` и `tts` с их параметрами.
Голосовой ввод/вывод поддерживает два подмодуля: STT (Speech‑to‑Text) и TTS (Text‑to‑Speech).

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `enabled` | `bool` | Включить голосовой ввод/вывод. | `true` |

### Параметры STT (Speech‑to‑Text)

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `model_size` | `string` | Размер модели Speech‑to‑Text. | `"large-v3"` |
| `device` | `string` | Устройство (`cpu`, `cuda`). | `"cuda"` |
| `compute_type` | `string` | Тип вычислений (`int8`, `float16`, …). | `"float16"` |

### Параметры TTS (Text-to-Speech)

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `model_path` | `string` | Путь к модели Text‑to‑Speech. | `"speech-text/model.pt"` |
| `speaker` | `string` | Идентификатор спикера. | `"xenia"` |
| `sample_rate` | `int` | Частота дискретизации (Гц). | `48000` |

### Общие параметры

| Параметр | Тип | Описание | Значение по умолчанию |
|----------|------|----------|----------------------|
| `reply_with_voice` | `bool` | Отправлять ответ голосом. | `true` |

---

## Раздел `providers`

Параметры провайдеров API задаются в виде вложенных объектов.

### `lm-studio`

| Параметр | Тип | Описание | Пример |
|----------|------|----------|--------|
| `base_url` | `string` | URL‑адрес API. | `"http://192.168.3.2:1234/v1"` |
| `api_key` | `string` | Ключ API (необходимо указать реальный ключ, если сервер требует аутентификацию). | `"lm-studio"` |
| `timeout` | `int` | Таймаут (сек). | `300` |
| `max_retries` | `int` | Количество повторов при ошибке. | `3` |

### `openrouter`

| Параметр | Тип | Описание | Пример |
|----------|------|----------|--------|
| `base_url` | `string` | URL‑адрес API. | `"https://openrouter.ai/api/v1"` |
| `api_key_env` | `string` | Имя переменной окружения с ключом. | `"OPENROUTER_API_KEY"` |
| `timeout` | `int` | Таймаут (сек). | `300` |
| `max_retries` | `int` | Количество повторов. | `3` |
| `streaming_enabled` | `bool` | Включить потоковый вывод. |
 - |

| `reasoning` | `section` | Параметры reasoning для модели (см. agent_factory) | |

### `anthropic`

| Параметр | Тип | Описание | Пример |
|----------|------|----------|--------|
| `base_url` | `string` | URL‑адрес API. | `"https://api.anthropic.com/v1/"` |
| `api_key_env` | `string` | Имя переменной окружения с ключом. | `"ANTHROPIC_API_KEY"` |
| `timeout` | `int` | Таймаут (сек). | `300` |
| `max_retries` | `int` | Количество повторов. | `3` |
| `streaming_enabled` | `bool` | Включить потоковый вывод. | `true` |

---

## Раздел `models`

Каждая модель описывается в виде объекта со следующими полями:

| Параметр | Тип | Описание |
|----------|------|----------|
| `provider` | `string` | Идентификатор провайдера (см. раздел `providers`). | - |
| `temperature` | `float` | Параметр «температуры» генерации. | - |
| `max_tokens` | `int` | Максимальное количество токенов в ответе. | - |
| `streaming_enabled` | `bool` | Включить потоковый вывод. |

**Пример:**

```yaml
models:
  kimi-k2.5:
    provider: openrouter
    temperature: 0.7
    max_tokens: 8192
    streaming_enabled: true
```

---

## Раздел `checkers`

Конфигурация модулей проверки (audit/checkers) для пост-обработки вывода агентов.

**Назначение:**
- Проверка ответов на безопасность, compliance, качество.
- Автоматический аудит перед отправкой пользователю.
- Поддержка нескольких checkers (default_audit и кастомные).

**Как работает:**
1. Агент генерирует ответ.
2. Если checker включен (по умолчанию), ответ отправляется на аудит-промпт.
3. Модель-auditor возвращает verdict (pass/fail) + explanation.
4. При fail — запрос на перегенерацию или блокировка.

### default_audit

Дефолтный аудитор для базовой проверки.

| Параметр | Тип | Описание | Пример |
|----------|-----|----------|--------|
| `prompt` | `string` | JSON-структура промпта для аудит-модели. Должен возвращать `{"verdict": "pass"|"fail", "reason": "..."}`. | См. пример ниже |

**Пример конфигурации:**
```yaml
checkers:
  default_audit:
    prompt: |
      {
        "role": "system",
        "content": [
          {
            "type": "text",
            "text": "You are a safety auditor. Analyze the agent output:\n{output}\nReturn JSON: {\"verdict\": \"pass\"|\"fail\", \"reason\": \"...\"}.\nCheck for: harm, illegal, unsafe, biased content."
          }
        ]
      }
```

**Расширения:**
- Добавляйте кастомные checkers: `safety_checker: {prompt: "..."}`, `bias_checker: {...}`.
- В `settings` можно добавить `audit_model: "gpt-4o-mini"` для выбора модели-аудитора (если не указано — default_agent.model).

---

## Раздел `tools`

Набор MCP‑инструментов и агентов‑инструментов.

| Инструмент | Параметры | Описание |
|------------|-----------|----------|
| `terminal` | `server_command` | Команда запуска терминального сервера (пример: `npx @dillip285/mcp-terminal`). |
| `filesystem` | `server_command` | Команда запуска файлового сервера. |
| `git` | `server_command` | Команда запуска Git‑сервера. |
| `sequentialthinking` | `server_command` | Команда запуска сервера последовательного мышления. |
| `file_agent`, `git_agent`, `task_analyst`, `researcher` | `type: agent`, `prompt_addition`, `context_strategy` | Агентские инструменты с пользовательскими подсказками и стратегиями контекста. |

**Пояснения к полям:**
- `type` — тип инструмента (`agent` или `tool`).
- `prompt_addition` — дополнительный промпт, который добавляется к инструкциям агента.
- `context_strategy` — стратегия использования контекста (`reuse`, `new`, и т.д.).


**Примечание:** `type` может принимать значения `agent` или `tool`; `prompt_addition` — дополнительный промпт; `context_strategy` — стратегия использования контекста (например, `reuse`, `new`).

---

## Переменные окружения

| Переменная | Описание | Пример значения |
|------------|----------|-----------------|
| `OPENROUTER_API_KEY` | Ключ API OpenRouter. | `sk-...` |
| `ANTHROPIC_API_KEY` | Ключ API Anthropic. | `sk-...` |
| `GOOGLE_API_KEY` | Ключ Google API (необязательно). | `AIza...` |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram‑бота. | `123456:ABC-DEF...` |
| `GRID_DEBUG` | Флаг отладки системы. | `true` / `false` |
| `GRID_LOG_LEVEL` | Уровень логирования (`INFO`, `DEBUG`, …). | `INFO` |
| `OPENAI_API_KEY` *(опционально)* | Ключ API OpenAI (если добавлен провайдер). | `sk-...` |
| `OPENAI_BASE_URL` *(опционально)* | Базовый URL для OpenAI. | `https://api.openai.com/v1` |
| `ANTHROPIC_BASE_URL` *(опционально)* | Базовый URL для Anthropic. | `https://api.anthropic.com/v1/` |

---

## Примеры конфигурации

### Минимальный пример `config.yaml`

```yaml
settings:
  default_agent: chat_agent
  max_history: 50
  max_turns: 100
  agent_timeout: 600
  debug: false
  max_tool_output_tokens: 8000
  mcp_enabled: true
  working_directory: "./"
  config_directory: "./"
  logs_directory: "./logs"
  allow_path_override: true
  
  agent_logging:
    enabled: true
    level: full
    save_prompts: true
    save_conversations: true
    save_executions: true
  
  image_processing:
    enabled: true
    auto_resize: true
    max_width: 1920
    max_height: 1080
    max_file_size_mb: 10
    jpeg_quality: 85

isolation:
  enabled: true
  type: docker
  image: grid-agent:latest

telegram:
  token_env: TELEGRAM_BOT_TOKEN
  polling_timeout: 30
  workspace_path: "./workspace"
  persist_path: "./data"
  max_message_history: 15
  memory_commands_enabled: true
  enable_transparency: false
  show_tool_calls: false
  allowed_users: null
  max_concurrent_tasks_per_user: 1
  progress_update_interval: 2.0

voice:
  enabled: true
  stt:
    model_size: large-v3
    device: cuda
    compute_type: float16
  tts:
    model_path: "speech-text/model.pt"
    speaker: xenia
    sample_rate: 48000
  reply_with_voice: true

providers:
  lm-studio:
    base_url: "http://192.168.3.2:1234/v1"
    api_key: lm-studio
    timeout: 300
    max_retries: 3
  openrouter:
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: OPENROUTER_API_KEY
    timeout: 300
    max_retries: 3
    streaming_enabled: true
  anthropic:
    base_url: "https://api.anthropic.com/v1/"
    api_key_env: ANTHROPIC_API_KEY
    timeout: 300
    max_retries: 3
    streaming_enabled: true

models:
  kimi-k2.5:
    provider: openrouter
    temperature: 0.7
    max_tokens: 8192
    streaming_enabled: true

checkers:
  default_audit:
    prompt: |
      {
        "role": "system",
        "content": [
          {
            "type": "text",
            "text": "You are a safety auditor. Analyze: {output}. JSON: {\"verdict\": \"pass\"/\"fail\", \"reason\": \"...\"}."
          }
        ]
      }

tools:
  terminal:
    server_command: "npx @dillip285/mcp-terminal"
  filesystem:
    server_command: "npx @modelcontextprotocol/server-filesystem"
  git:
    server_command: "npx @cyanheads/git-mcp-server"
  sequentialthinking:
    server_command: "npx @modelcontextprotocol/server-sequential-thinking"
  file_agent:
    type: agent
    prompt_addition: "..."
    context_strategy: "..."
  git_agent:
    type: agent
    prompt_addition: "..."
    context_strategy: "..."
  task_analyst:
    type: agent
    prompt_addition: "..."
    context_strategy: "..."
  researcher:
    type: agent
    prompt_addition: "..."
    context_strategy: "..."
```

---

## Примечания

- Все пути, указанные в параметрах `server_command`, должны быть доступными из рабочей директории проекта.
- При использовании Docker‑изоляции убедитесь, что образ `grid-agent:latest` существует локально или в реестре.
- Переменные окружения, указанные в `token_env` и `api_key_env`, должны быть заданы в файле `.env` или в окружении системы, где запускается приложение.
- Параметр `settings.max_tool_output_tokens` ограничивает размер вывода инструментов; при превышении генерируется ошибка.
- Для отладки включите `settings.debug` и настройте `GRID_LOG_LEVEL`.
- Параметры `voice.stt.device` и `voice.stt.compute_type` должны быть согласованы (cuda → float16, cpu → int8).

---

*Документация подготовлена автоматически на основе анализа конфигурации проекта.*
