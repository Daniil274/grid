# Security Analysis: Аутентификация, Авторизация, Сессии

> **Дата анализа:** 2026-05-17  
> **Область:** API-ключи, сессии, Telegram-интеграция, MCP-серверы, контроль доступа  
> **Проект:** grid (AI-агентный фреймворк)

---

## Находка 1: API-ключи в .env — скомпрометированы в репозитории
- **Серьёзность:** CRITICAL
- **Файл:** `.env` (строки 5, 12, 14)
- **Описание:**
  Файл `.env` содержит три реальных API-ключа в открытом виде и закоммичен в репозиторий:
  - `OPENROUTER_API_KEY=sk-or-v1-...` — ключ OpenRouter с доступом к десяткам LLM-провайдеров
  - `TELEGRAM_BOT_TOKEN=8138042180:...` — токен Telegram бота (полный контроль над ботом)
  - `OPENCODE_API_KEY=sk-qiMwZ...` — ключ OpenCode
  Хотя `.gitignore` вероятно содержит `.env`, файл уже находится в истории Git. Ключи необходимо немедленно отозвать и перевыпустить.
- **Код:** `.env` в корне проекта, строки 5, 12, 14.
- **Рекомендация:**
  1. Немедленно отозвать все три ключа через соответствующие панели управления.
  2. Перевыпустить ключи и поместить их в `.env` локально (вне репозитория).
  3. Использовать `git filter-branch` или `BFG Repo-Cleaner` для удаления `.env` из всей истории.
  4. Проверить `.gitignore` на наличие `.env`.

---

## Находка 2: get_api_key() — кеширование отсутствует, нет маскирования в логах
- **Серьёзность:** MEDIUM
- **Файл:** `core/config/config.py:314-327`
- **Описание:**
  Функция `get_api_key()` не кеширует результат — при каждом вызове происходит обращение к `os.getenv()` и/или чтение поля `provider.api_key`. Хотя прямого логирования ключа нет (логируется только warning `"No API key found"`), отсутствие кеширования увеличивает поверхность атаки: ключ многократно извлекается из переменных окружения и передаётся в разные компоненты.
- **Код:**
  ```python
  def get_api_key(self, provider_key: str) -> Optional[str]:
      provider = self.get_provider(provider_key)
      if provider.api_key:
          return provider.api_key       # <-- ключ в открытом виде из конфига
      if provider.api_key_env:
          api_key = os.getenv(provider.api_key_env)
          if api_key:
              return api_key            # <-- ключ из env
      logger.warning(f"No API key found for provider '{provider_key}'")
      return None
  ```
- **Рекомендация:**
  1. Добавить `@lru_cache` или внутренний `_api_key_cache: dict` для кеширования уже извлечённых ключей.
  2. При логировании ошибок маскировать значение ключа: `key[:4] + "****"` при логировании `provider.api_key`.
  3. Убрать поддержку `provider.api_key` (прямой ключ в конфиге) — оставить только `api_key_env`.

---

## Находка 3: AsyncOpenAI клиент — api_key передаётся в конструктор без маскирования
- **Серьёзность:** MEDIUM
- **Файлы:** `core/agent_factory.py:790-820`, `core/managers/model_manager.py:62-82`
- **Описание:**
  При создании `AsyncOpenAI` клиента `api_key` передаётся как именованный параметр в `**kwargs`. Если включено debug-логирование OpenAI SDK или httpx, ключ может попасть в логи запросов. В коде фабрики нет явной фильтрации ключа из trace-событий. Дополнительно: в `agent_factory.py:907-913` при ошибке отсутствия ключа в `AgentError` попадает `env_var` (имя переменной), но не сам ключ — это корректно.
- **Код:**
  ```python
  # core/agent_factory.py:791-799
  kwargs: Dict[str, Any] = dict(
      api_key=api_key,          # <-- ключ передаётся напрямую
      base_url=base_url,
      timeout=timeout,
      max_retries=max_retries,
  )
  return AsyncOpenAI(**kwargs)
  ```
- **Рекомендация:**
  1. Отключить `trust_env=False` уже установлено — это хорошо.
  2. Добавить фильтр в `httpx` logging: установить `logging.getLogger("httpx").setLevel(logging.WARNING)`.
  3. Рассмотреть использование `openai.DefaultHttpxClient` с кастомным логгером, который маскирует заголовок `Authorization`.

---

## Находка 4: ProviderConfig — нет валидации на пустой api_key
- **Серьёзность:** LOW
- **Файл:** `schemas/schemas.py:17-23`
- **Описание:**
  Поля `api_key` и `api_key_env` в `ProviderConfig` объявлены как `Optional[str]` без валидаторов, проверяющих что хотя бы одно из них заполнено или что значение не является пустой строкой. Можно создать провайдера с `api_key: ""` или `api_key_env: ""`, и ошибка будет обнаружена только во время выполнения.
- **Код:**
  ```python
  class ProviderConfig(BaseModel):
      name: str
      base_url: str
      api_key_env: Optional[str] = None   # нет валидации
      api_key: Optional[str] = None        # нет валидации
      timeout: int = Field(default=30, ge=1, le=300)
      max_retries: int = Field(default=2, ge=0, le=10)
  ```
- **Рекомендация:**
  Добавить `@model_validator` который проверяет, что хотя бы одно из полей (`api_key`, `api_key_env`) непустое:
  ```python
  @model_validator(mode="after")
  def check_api_key(self) -> "ProviderConfig":
      if not self.api_key and not self.api_key_env:
          raise ValueError("Either api_key or api_key_env must be set")
      return self
  ```

---

## Находка 5: SQLiteSession — сессии изолированы по (agent_key, context_id), но context_id не привязан к пользователю
- **Серьёзность:** HIGH
- **Файлы:** `core/managers/session_manager.py:31-59`, `core/agent_factory.py:487-489`
- **Описание:**
  Сессии агентов кешируются в словаре `_agent_sessions: Dict[Tuple[str, str], SQLiteSession]` с ключом `(agent_key, context_id)`. Однако `context_id` генерируется произвольно и **не включает `user_id`**. Это означает, что если два разных пользователя случайно получат одинаковый `context_id`, они будут разделять одну сессию агента. Вероятность коллизии низкая (используются случайные UUID-подобные идентификаторы вроде `ctx-69f8fe40`), но архитектурно изоляция не гарантирована.
  Дополнительно: `SQLiteSession` сохраняется в единую БД `logs/agent_sessions.db` для всех пользователей — нет разделения по `user_id`.
- **Код:**
  ```python
  # session_manager.py:31-35
  def get_agent_session(self, agent_key: str, context_id: str) -> SQLiteSession:
      session_key = (agent_key, context_id)  # <-- нет user_id
      if session_key not in self._agent_sessions:
          session_id = f"agent_{agent_key}_{context_id}"
          self._agent_sessions[session_key] = self._session_factory(session_id)
  ```
- **Рекомендация:**
  1. Включить `user_id` в ключ сессии: `session_key = (user_id, agent_key, context_id)`.
  2. Для `_create_persistent_sqlite_session` использовать отдельные БД или включать `user_id` в `session_id`.
  3. Добавить TTL для неактивных сессий и автоматическую очистку.

---

## Находка 6: MCP-серверы — server_command из конфига без валидации путей
- **Серьёзность:** HIGH
- **Файл:** `core/managers/mcp_manager.py:311-360`
- **Описание:**
  `server_command` для MCP-серверов читается из `ToolConfig.server_command` (YAML-конфиг) и напрямую передаётся в `MCPServerStdio` → `subprocess`. **Нет валидации**, что исполняемый файл находится в ожидаемом месте (например, `npx` может быть подменён через `PATH`). В случае Docker-изоляции команда оборачивается в `docker exec`, но если `container_id` не задан, `command` исполняется напрямую на хосте.
  Дополнительно: `env_vars` из конфига передаются в процесс — потенциальный вектор инъекции переменных окружения (например, `LD_PRELOAD`).
- **Код:**
  ```python
  # mcp_manager.py:328-330
  server_command = tool_config.server_command or []
  command = server_command[0]       # <-- не валидируется
  args = list(server_command[1:])   # <-- не валидируется
  # ...
  server = ResilientMCPServerStdio(
      params={"command": command, "args": args, ...},
  )
  ```
- **Рекомендация:**
  1. Валидировать `command` по белому списку разрешённых исполняемых файлов.
  2. Использовать `shutil.which()` с абсолютным путём, а не полагаться на `PATH`.
  3. Очищать `env_vars` от опасных переменных: `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONSTARTUP`.
  4. Всегда запускать MCP-серверы в Docker-контейнере (с проверкой `container_id`).

---

## Находка 7: Telegram-интеграция — код отсутствует, конфиг содержит токен
- **Серьёзность:** MEDIUM
- **Файлы:** `config.yaml:44-62`, `tests/test_telegram_progress_observer.py`
- **Описание:**
  В `config.yaml` присутствует секция `telegram:` с `token_env: TELEGRAM_BOT_TOKEN`, `allowed_users:` (пустой список), `workspace_path`, `polling_timeout` и другими настройками. Однако код Telegram-бота **отсутствует** в репозитории — директория `examples/telegram_bot` не существует, несмотря на ссылки в тестах и `timeline/integration.py`. При этом тест `test_telegram_progress_observer.py` импортирует `TelegramProgressObserver` из `examples.telegram_bot.telegram_progress_observer`.
  Это означает, что либо код был удалён, либо ещё не написан, но конфигурация активна.
  **Критично:** `allowed_users` пуст — если бот заработает, он будет принимать сообщения от любого пользователя Telegram.
- **Код:**
  ```yaml
  # config.yaml:44-62
  telegram:
    token_env: TELEGRAM_BOT_TOKEN
    allowed_users:          # <-- пустой список = нет ограничений
    max_concurrent_tasks_per_user: 1
  ```
- **Рекомендация:**
  1. Восстановить или создать код Telegram-бота с проверкой `allowed_users`.
  2. По умолчанию, если `allowed_users` пуст — **отклонять все сообщения**, а не пропускать всех.
  3. Добавить валидацию ID отправителя через `message.from_user.id`.
  4. Отозвать скомпрометированный `TELEGRAM_BOT_TOKEN` (см. Находку 1).

---

## Находка 8: User context.json — агентские инструкции сохраняются в открытом виде
- **Серьёзность:** LOW
- **Файл:** `data/user_790241050/context.json`
- **Описание:**
  Файл `context.json` содержит полную историю диалогов пользователя, включая системные промпты агентов (`agent_instructions`) и содержимое сообщений. Данные хранятся в JSON без шифрования. Хотя это локальный файл, при развёртывании на сервере он может быть доступен другим процессам. `user_id=790241050` — это реальный Telegram user ID, который раскрывает личность пользователя.
- **Код:**
  ```json
  {
    "active_context_id": "ctx-69f8fe40",
    "contexts": {
      "ctx-5420792f": {
        "conversation_history": [...],
        "metadata": {
          "user_id": "790241050",
          "agent_instructions": "Ты главный чат-агент системы..."
        }
      }
    }
  }
  ```
- **Рекомендация:**
  1. Не хранить `user_id` в открытом виде в именах файлов и внутри JSON.
  2. Шифровать `context.json` (например, через `cryptography.fernet` с ключом из переменной окружения).
  3. Установить `chmod 600` на файлы в `data/`.

---

## Находка 9: PermissionChecker — работает на ролях, не интегрирован с аутентификацией пользователей
- **Серьёзность:** MEDIUM
- **Файл:** `core/platform/governance.py:21-33`
- **Описание:**
  `PermissionChecker.assert_allowed()` проверяет права роли на действие через `policy.roles`. Однако **нет привязки ролей к конкретным пользователям** — любой код, вызывающий `assert_allowed`, может передать произвольную роль. Система permissions существует изолированно от системы пользователей: нет механизма аутентификации, который бы сопоставлял пользователя с ролью.
- **Код:**
  ```python
  class PermissionChecker:
      def assert_allowed(self, policy: PermissionPolicy, role: str, action: str) -> None:
          role_policy = policy.roles.get(role)
          if role_policy is None:
              raise PermissionDeniedError(f"Role '{role}' is not configured")
          if "*" in role_policy.deny or action in role_policy.deny:
              raise PermissionDeniedError(...)
          if "*" in role_policy.allow or action in role_policy.allow:
              return
          raise PermissionDeniedError(...)
  ```
- **Рекомендация:**
  1. Добавить `UserRegistry`, связывающий `user_id` → `role`.
  2. `assert_allowed` должен принимать `user_id` и самостоятельно определять роль.
  3. Для CLI-режима (без пользователей) использовать фиксированную роль `"admin"`.

---

## Находка 10: BudgetTracker — in-memory, сброс при перезапуске
- **Серьёзность:** LOW
- **Файл:** `core/platform/governance.py:37-128`
- **Описание:**
  `BudgetTracker` хранит все счётчики в оперативной памяти (`self._new_systems`, `self._promotions`, etc.). При перезапуске процесса все лимиты сбрасываются. Злоумышленник может обойти бюджетные ограничения, перезапустив приложение. Нет персистентного хранения.
- **Код:**
  ```python
  class BudgetTracker:
      def __init__(self) -> None:
          self._lock = RLock()
          self._new_systems = 0     # <-- сбрасывается при перезапуске
          self._promotions = 0
  ```
- **Рекомендация:**
  1. Сохранять состояние бюджета в SQLite (например, в `data/budget.db`).
  2. Использовать монотонно возрастающий счётчик с окном, основанным на реальном времени.

---

## Находка 11: MCP server — подстановка proxy-переменных окружения
- **Серьёзность:** LOW
- **Файл:** `core/managers/mcp_manager.py:384-396`
- **Описание:**
  При запуске MCP-сервера в Docker-контейнере код пробрасывает переменные окружения прокси с хоста:
  ```python
  for _proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", ...):
      _proxy_val = _os.environ.get(_proxy_var)
      if _proxy_val:
          env.setdefault(_proxy_var, _proxy_val)
  ```
  Это может привести к утечке учётных данных прокси (если они встроены в URL) внутрь контейнера, где их может прочитать скомпрометированный MCP-сервер.
- **Рекомендация:**
  1. Маскировать пароли в URL прокси перед передачей: `http://user:****@host:port`.
  2. Использовать отдельные переменные для учётных данных прокси.

---

## Находка 12: Отсутствует rate-limiting на API-ключи
- **Серьёзность:** MEDIUM
- **Файлы:** `core/agent_factory.py`, `core/managers/model_manager.py`
- **Описание:**
  Нет механизма ограничения частоты запросов к LLM API. Злоумышленник, получивший доступ к агенту (через незащищённый Telegram-бот или CLI), может исчерпать квоту API-ключа, что приведёт к финансовым потерям и отказу в обслуживании.
- **Рекомендация:**
  1. Добавить `BudgetTracker` для API-вызовов (количество запросов/токенов в минуту/день).
  2. Настроить лимиты на стороне провайдера (OpenRouter позволяет установить лимиты расходов).

---

## ИТОГО: 12 находок (1 CRITICAL, 2 HIGH, 5 MEDIUM, 4 LOW)

| # | Серьёзность | Краткое описание |
|---|-------------|------------------|
| 1 | **CRITICAL** | Реальные API-ключи в `.env` закоммичены в репозиторий |
| 2 | MEDIUM | `get_api_key()` без кеширования, без маскирования |
| 3 | MEDIUM | `api_key` передаётся в `AsyncOpenAI` без фильтрации логов |
| 4 | LOW | `ProviderConfig` не валидирует пустой api_key |
| 5 | **HIGH** | `SQLiteSession` изолирована без `user_id` в ключе |
| 6 | **HIGH** | `server_command` MCP-серверов без валидации путей |
| 7 | MEDIUM | Telegram-код отсутствует, `allowed_users` пуст |
| 8 | LOW | `context.json` хранит user_id и переписку в открытом виде |
| 9 | MEDIUM | `PermissionChecker` не привязан к пользователям |
| 10 | LOW | `BudgetTracker` теряет состояние при перезапуске |
| 11 | LOW | Proxy-переменные пробрасываются в MCP-контейнер без маскирования |
| 12 | MEDIUM | Отсутствует rate-limiting API-вызовов |
