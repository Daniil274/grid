# Claude Tools Example

Адаптация инструментов OpenClaude для Grid Agent System.

## Возможности

- 🖥️ **Bash Tool** — выполнение shell-команд с безопасностью
- 📁 **File Tools** — чтение, запись, редактирование файлов
- 🔍 **Search Tools** — glob и grep для поиска
- 🌐 **Web Tools** — загрузка страниц и веб-поиск
- 📓 **Notebook Tool** — работа с Jupyter notebooks
- ✅ **Todo Tool** — управление задачами

## Провайдеры и модели

Конфигурация поддерживает несколько провайдеров:

| Провайдер | Модель | Агент | Переменная окружения |
|-----------|--------|-------|---------------------|
| OpenAI | gpt-4o | `claude_engineer` | `OPENAI_API_KEY` |
| OpenAI | gpt-4o-mini | `file_assistant` | `OPENAI_API_KEY` |
| OpenCode | kimi-k2.5-opencode | `kimi_engineer` | `OPENCODE_API_KEY` |
| OpenCode | glm-5-opencode | `glm_engineer` | `OPENCODE_API_KEY` |

## Установка

```bash
cd examples/claude-tools

# Настройка переменных окружения
cp .env.example .env
# Отредактируйте .env и добавьте ваш API ключ

# Установка зависимостей
pip install -r requirements.txt
```

## Переменные окружения

Создайте файл `.env` (скопируйте из `.env.example`):

```bash
cp .env.example .env
```

**Обязательно (выберите один провайдер):**

### OpenAI
```bash
OPENAI_API_KEY=sk-your_openai_api_key_here
```

### OpenCode
```bash
OPENCODE_API_KEY=your_opencode_api_key_here
```

**Опционально:**
- `FIRECRAWL_API_KEY` — для веб-поиска (получить на [firecrawl.dev](https://firecrawl.dev))
- `CLAUDE_TODO_FILE` — кастомный путь к файлу задач

## Запуск

### С OpenAI (по умолчанию)

```bash
# Из корня grid
python agent_chat.py --config examples/claude-tools/config.yaml

# Или напрямую
python ../../agent_chat.py --config config.yaml --agent claude_engineer
```

### С OpenCode

```bash
# Использовать Kimi K2.5
python ../../agent_chat.py --config config.yaml --agent kimi_engineer

# Использовать GLM-5
python ../../agent_chat.py --config config.yaml --agent glm_engineer
```

### Однократный запрос

```bash
python ../../agent_chat.py --config config.yaml --agent kimi_engineer -m "Найди все Python файлы"
```

## Примеры использования

### Работа с файлами

```
Создай файл hello.py с функцией greet(name) которая возвращает "Hello, {name}!"
```

### Поиск

```
Найди все Python файлы в проекте используя glob
Найди где определена функция bash_tool используя grep
```

### Веб

```
Загрузи страницу https://docs.python.org/3/ и покажи заголовки
```

### Todo

```
Создай задачу: "Реализовать функцию X"
Отметь задачу как выполненную
Покажи все задачи
```

## Структура

```
claude-tools/
├── .env.example         # Шаблон переменных окружения
├── .gitignore           # Исключения для git
├── README.md            # Документация
├── config.yaml          # Конфигурация агентов и провайдеров
├── requirements.txt     # Зависимости
├── test_tools.py        # Тестовый скрипт
└── tools/               # Инструменты
    ├── __init__.py      # Регистрация
    ├── bash_tool.py     # Shell команды
    ├── file_tools.py    # Файловые операции
    ├── search_tools.py  # Поиск
    ├── web_tools.py     # Веб
    ├── notebook_tool.py # Jupyter
    └── todo_tool.py     # Задачи
```

## Агенты
### kimi_engineer (kimi-k2.5-opencode)
Инженер с полным набором инструментов через OpenCode API.

### glm_engineer (glm-5-opencode)
Инженер с полным набором инструментов через OpenCode API.

## Безопасность

BashTool включает проверку опасных команд:
- `rm -rf /` и подобные
- Перезапись системных файлов
- Выполнение без проверки

При необходимости расширьте список в `tools/bash_tool.py`.
