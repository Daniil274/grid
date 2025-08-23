# Настройка и использование Grid MCP Server

## Проблемы, которые были решены

Ваша Grid система изначально была настроена как **MCP клиент** (использует внешние MCP серверы), но не имела собственного **MCP сервера** для доступа к агентам из других приложений.

### Основные проблемы:
1. ❌ **Отсутствие MCP сервера** - нельзя было использовать Grid агентов из Claude Code
2. ❌ **Ошибки с именами инструментов** - конфликты с каналами в именах инструментов  
3. ❌ **Проблемы с путями** - неправильные рабочие директории для Windows
4. ❌ **Зависание при статусе** - ошибки в получении информации о системе

### Решения:
1. ✅ **Создан полноценный MCP сервер** (`mcp_server.py`)
2. ✅ **Исправлены ошибки инструментов** - добавлена обработка ошибок
3. ✅ **Улучшена обработка статуса** - безопасное получение информации
4. ✅ **Добавлена документация и тесты**

## Быстрый старт

### 1. Установка зависимостей
```bash
pip install mcp>=1.0.0
# или
pip install -r requirements.txt
```

### 2. Запуск MCP сервера
```bash
python start_mcp_server.py
```

### 3. Тестирование
```bash
python quick_test_mcp.py
```

Если всё работает, вы увидите:
```
🧪 Quick MCP Server Test
✅ Initialized
✅ Found 54 resources
✅ Found 12 agent tools, 3 system tools
✅ get_grid_status works
✅ Simple agent works: 2 chars response
🎉 MCP Server is working!
```

## Настройка Claude Code

Добавьте в конфигурацию Claude Code:

### Файл конфигурации MCP
Создайте или отредактируйте `~/.config/claude-code/mcp_servers.json`:

```json
{
  "mcpServers": {
    "grid-agents": {
      "command": "python",
      "args": [
        "/workspaces/grid/start_mcp_server.py",
        "--config", "/workspaces/grid/config.yaml"
      ],
      "env": {}
    }
  }
}
```

### Альтернативная конфигурация
Если хотите использовать напрямую сервер:

```json
{
  "mcpServers": {
    "grid-agents": {
      "command": "python",
      "args": [
        "/workspaces/grid/mcp_server.py",
        "--config", "/workspaces/grid/config.yaml",
        "--log-level", "INFO"
      ],
      "env": {}
    }
  }
}
```

## Доступные инструменты

После подключения в Claude Code будут доступны следующие инструменты:

### Агенты (12 инструментов)
- `run_agent_assistant` - Базовый помощник для общения
- `run_agent_simple_test` - Простой тестовый агент  
- `run_agent_file_agent` - Специалист по работе с файлами
- `run_agent_git_agent` - Полнофункциональный помощник разработчика для Git
- `run_agent_researcher` - Исследователь для анализа файлов и документов
- `run_agent_full_agent` - Универсальный помощник со всеми инструментами
- `run_agent_context_wrapper` - Сборщик контекста
- `run_agent_code_agent` - Специалист по программированию и анализу кода
- `run_agent_thinker` - Агент для глубокого анализа и размышлений
- `run_agent_task_analyst` - Аналитик задач
- `run_agent_debugger` - Агент для отладки кода
- `run_agent_coordinator` - Координатор команды специализированных агентов

### Системные инструменты (3 инструмента)
- `list_grid_agents` - Список всех доступных агентов
- `get_grid_status` - Статус и информация о системе Grid
- `clear_grid_context` - Очистка контекста разговора

## Примеры использования в Claude Code

```
# Анализ проекта
Используй run_agent_coordinator для анализа структуры этого проекта

# Работа с кодом
Запусти run_agent_code_agent чтобы он проанализировал код в файле main.py

# Работа с файлами  
Используй run_agent_file_agent для создания нового файла с документацией

# Работа с Git
Вызови run_agent_git_agent чтобы проверить статус репозитория

# Получение информации о системе
Покажи статус Grid системы и список всех агентов
```

## Мониторинг и отладка

### Логи
MCP сервер выводит подробные логи:
```bash
python start_mcp_server.py --log-level DEBUG
```

### Тестирование подключения
```bash
# Быстрый тест
python quick_test_mcp.py

# Полный тест (может занять время)
python test_mcp_server.py
```

### Проверка MCP инструментов
После подключения к Claude Code можете проверить:
```
Покажи все доступные MCP инструменты
```

## Решение проблем

### MCP сервер не запускается
1. Проверьте зависимости: `pip list | grep mcp`
2. Проверьте конфигурацию: `python -c "from core.config import Config; Config('config.yaml')"`
3. Запустите с отладкой: `python start_mcp_server.py --log-level DEBUG`

### Агенты не отвечают или дают ошибки
1. Проверьте API ключи в переменных окружения
2. Некоторые агенты требуют MCP инструменты, которые могут быть недоступны
3. Используйте простые агенты: `run_agent_simple_test` для тестирования

### Claude Code не видит инструменты
1. Перезапустите Claude Code после добавления конфигурации
2. Проверьте пути в конфигурации MCP
3. Убедитесь, что MCP сервер запущен и работает

## Архитектура решения

```
Claude Code (MCP Client)
    ↓ MCP Protocol
Grid MCP Server (mcp_server.py)  
    ↓ Internal API
Grid Agent System (agent_factory.py)
    ↓ OpenAI Agents SDK
LLM Providers (OpenRouter, LM Studio, etc.)
```

Grid MCP Server действует как мост между Claude Code и внутренней системой агентов, предоставляя стандартный MCP интерфейс для доступа ко всем возможностям Grid системы.

## Что дальше?

1. **Используйте агентов** - Теперь вы можете запускать Grid агентов прямо из Claude Code
2. **Настройте рабочую директорию** - Исправьте путь в `config.yaml` на актуальный
3. **Добавьте API ключи** - Настройте переменные окружения для всех провайдеров
4. **Экспериментируйте** - Попробуйте разные агенты для разных задач

Grid система теперь полностью интегрирована с экосистемой MCP! 🚀