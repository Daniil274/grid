# OpenRouter Agents Python

Полнофункциональная система AI агентов с поддержкой путей, конфигурации и MCP серверов.

## 🚀 Возможности

- **Поддержка путей**: Агенты получают информацию о рабочей директории и контекстных путях
- **YAML конфигурация**: Гибкая настройка провайдеров, моделей, инструментов и агентов
- **MCP серверы**: Интеграция с Model Context Protocol
- **Множественные агенты**: Специализированные агенты для разных задач
- **Инструменты**: Файловые операции, Git, веб-поиск и многое другое

## 📁 Поддержка путей

Система поддерживает передачу путей агенту несколькими способами:

### 1. Рабочая директория
Агент получает информацию о рабочей директории, относительно которой выполняются операции:

```bash
# Запуск с указанием рабочей директории
python agent_chat.py --path /path/to/project

# Или через main.py
python main.py --path /path/to/project
```

### 2. Контекстный путь
Можно передать дополнительный контекстный путь для конкретной задачи:

```bash
# Передача контекстного пути
python agent_chat.py --context-path src/components

# Комбинирование путей
python agent_chat.py --path /path/to/project --context-path docs
```

### 3. Конфигурация путей
В `config.yaml` можно настроить пути по умолчанию:

```yaml
settings:
  working_directory: "."  # Рабочая директория по умолчанию
  config_directory: "."   # Директория конфигурации
  allow_path_override: true  # Разрешить переопределение пути при запуске
```

## 🛠️ Установка

1. Клонируйте репозиторий:
```bash
git clone <repository-url>
cd openai-agents-python
```

2. Установите зависимости:
```bash
pip install -r requirements.txt
```

3. Настройте API ключи в переменных окружения:
```bash
export OPENROUTER_API_KEY="your-api-key"
export OPENAI_API_KEY="your-openai-key"
export ANTHROPIC_API_KEY="your-anthropic-key"
```

4. Скопируйте и настройте конфигурацию:
```bash
cp config.yaml.example config.yaml
# Отредактируйте config.yaml под свои нужды
```

## 🚀 Использование

### Базовый запуск
```bash
# Запуск с агентом по умолчанию
python agent_chat.py

# Запуск конкретного агента
python agent_chat.py --agent file_agent

# Запуск с рабочей директорией
python agent_chat.py --path /path/to/project
```

### Примеры использования путей

#### Работа с файлами в конкретной директории
```bash
# Агент будет работать в директории проекта
python agent_chat.py --path /path/to/my-project --agent file_agent

# Затем можно спросить:
# "Покажи содержимое файла main.py"
# "Создай новый файл README.md"
```

#### Git операции в репозитории
```bash
# Git агент в конкретном репозитории
python agent_chat.py --path /path/to/git-repo --agent git_agent

# Команды:
# "Покажи статус репозитория"
# "Создай коммит с сообщением 'Update docs'"
```

#### Исследование с контекстным путем
```bash
# Исследователь с фокусом на конкретную директорию
python agent_chat.py --context-path src/utils --agent researcher

# Можно спросить:
# "Найди информацию о функциях в этой директории"
```

### Интерактивные команды

В интерактивном режиме доступны команды:
- `exit` - выход
- `clear` - очистка истории
- `paths` - показать информацию о путях

### Одноразовые команды
```bash
# Отправить одно сообщение и получить ответ
python agent_chat.py --message "Покажи содержимое файла config.yaml" --path /path/to/project
```

## 🤖 Доступные агенты

- **assistant** - Базовый помощник
- **file_agent** - Работа с файлами
- **git_agent** - Git операции
- **researcher** - Веб-поиск и исследования
- **thinker** - Глубокий анализ
- **coordinator** - Координатор команды агентов
- **full_agent** - Универсальный агент
- **mcp_agent** - Агент с MCP серверами

## 📋 Конфигурация

Основные настройки в `config.yaml`:

```yaml
settings:
  default_agent: "coordinator"
  max_history: 15
  debug: true
  mcp_enabled: true
  working_directory: "."
  config_directory: "."
  allow_path_override: true
```

## 🔧 Инструменты

Система поддерживает множество инструментов:
- Файловые операции (чтение, запись, поиск)
- Git операции (статус, коммиты, ветки)
- Веб-поиск
- MCP серверы
- Агенты как инструменты

## 📝 Примеры

### Работа с проектом
```bash
# Запуск в директории проекта
python agent_chat.py --path /path/to/project --agent file_agent

# Вопросы:
# "Покажи структуру проекта"
# "Найди все Python файлы"
# "Создай новый модуль utils.py"
```

### Git управление
```bash
# Git агент в репозитории
python agent_chat.py --path /path/to/repo --agent git_agent

# Команды:
# "Покажи изменения в файлах"
# "Создай новую ветку feature"
# "Сделай коммит изменений"
```

### Исследования
```bash
# Исследователь с контекстом
python agent_chat.py --context-path "machine learning" --agent researcher

# Вопросы:
# "Найди последние новости о GPT-4"
# "Исследуй лучшие практики Python"
```

## 🐛 Отладка

Включите отладку в `config.yaml`:
```yaml
settings:
  debug: true
```

Или используйте флаг при запуске:
```bash
python agent_chat.py --debug
```

## 📄 Лицензия

MIT License