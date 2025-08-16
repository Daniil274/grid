# Система тестирования Grid

Комплексная система тестирования для платформы агентов Grid, включающая модульные, интеграционные и функциональные тесты.

## 🗂️ Структура тестов

```
tests/
├── config_test.yaml         # Тестовая конфигурация агентов
├── test_framework.py        # Базовый фреймворк тестирования
├── test_agents.py          # Тесты агентов
├── test_tools.py           # Тесты инструментов
├── test_context.py         # Тесты системы контекста
├── test_integration.py     # Интеграционные тесты
├── run_tests.py           # Запускающий скрипт
└── README.md              # Данная документация
```

## 🚀 Быстрый старт

### Установка зависимостей

```bash
# Установка тестовых зависимостей
pip install pytest pytest-asyncio

# Установка основных зависимостей Grid
pip install -r requirements.txt
```

### Запуск всех тестов

```bash
# Из корневой директории проекта
python tests/run_tests.py

# Или через pytest
pytest tests/ -v
```

### Запуск конкретных типов тестов

```bash
# Только тесты агентов
python tests/run_tests.py --type agents

# Только тесты инструментов  
python tests/run_tests.py --type tools

# Только интеграционные тесты
python tests/run_tests.py --type integration

# Подробный вывод
python tests/run_tests.py --verbose
```

## 📋 Типы тестов

### 1. Тесты агентов (`test_agents.py`)

Проверяют функциональность агентов:

- **Базовые тесты**: Создание агентов, кеширование, базовые ответы
- **Функциональные тесты**: Работа с различными типами агентов
- **Тесты производительности**: Время ответа, параллельное выполнение
- **Интеграционные тесты**: End-to-end рабочие процессы

```python
# Пример теста агента
@pytest.mark.asyncio
async def test_simple_agent_response():
    async with TestEnvironment() as env:
        response = await env.agent_factory.run_agent(
            "test_simple_agent", 
            "Привет!"
        )
        assert response is not None
```

### 2. Тесты инструментов (`test_tools.py`)

Проверяют работу всех типов инструментов:

- **Файловые операции**: Чтение, запись, поиск, редактирование
- **Git операции**: Статус, коммиты, ветки
- **MCP серверы**: Конфигурация и подключение
- **Агентные инструменты**: Взаимодействие между агентами

```python
# Пример теста инструмента
@pytest.mark.asyncio
async def test_file_operations():
    result = await write_file("test.txt", "content")
    assert "успешно" in result.lower()
```

### 3. Тесты контекста (`test_context.py`)

Проверяют систему управления контекстом:

- **Управление историей**: Добавление, очистка, ограничения
- **Передача контекста**: Между агентами и инструментами
- **Сохранение**: Персистентность данных
- **Производительность**: Работа с большими объемами

```python
# Пример теста контекста
def test_context_retention():
    context_manager = ContextManager(max_history=10)
    context_manager.add_message("user", "Тест")
    assert len(context_manager.history) == 1
```

### 4. Интеграционные тесты (`test_integration.py`)

Проверяют взаимодействие компонентов:

- **Полные рабочие процессы**: End-to-end сценарии
- **Обработка ошибок**: Восстановление и каскадные ошибки  
- **Производительность системы**: Нагрузочное тестирование
- **Реальные сценарии**: Практические случаи использования

## ⚙️ Конфигурация тестов

### Тестовая конфигурация (`config_test.yaml`)

Изолированная среда для тестирования:

```yaml
settings:
  default_agent: "test_simple_agent"
  max_history: 10
  max_turns: 5
  agent_timeout: 30
  debug: true
  mcp_enabled: false  # Отключено для базовых тестов

# Тестовые агенты с минимальными зависимостями
agents:
  test_simple_agent:
    name: "Тестовый простой агент"
    model: "test_model"
    tools: []
    
  test_file_agent:
    name: "Тестовый файловый агент" 
    model: "test_model"
    tools: ["test_file_read", "test_file_write"]
```

### Переменные окружения для тестов

```bash
# Настройки тестирования
export GRID_TEST_ENV=true
export GRID_LOG_LEVEL=DEBUG

# Для интеграционных тестов с внешними сервисами
export RUN_INTEGRATION_TESTS=1
export TEST_LMSTUDIO_HOST=localhost
export TEST_LMSTUDIO_PORT=1234
```

## 🧪 Фреймворк тестирования

### TestEnvironment

Изолированная тестовая среда:

```python
async with TestEnvironment() as env:
    # Автоматическая настройка:
    # - Временная директория
    # - Тестовая конфигурация
    # - Мок провайдеры
    # - Очистка после завершения
    
    env.set_mock_responses(["Тестовый ответ"])
    response = await env.agent_factory.run_agent("agent", "message")
```

### AgentTestCase

Структурированные тестовые сценарии:

```python
test_case = AgentTestCase("test_name", "agent_key", "description")

test_case.setup(lambda env: setup_test_data(env))
test_case.test(lambda env: test_basic_response(env, "agent", "message"))
test_case.teardown(lambda env: cleanup_test_data(env))

result = await test_case.run(env)
```

### AgentTestSuite

Наборы связанных тестов:

```python
suite = AgentTestSuite("Suite Name")
suite.add_test(test_case1)
suite.add_test(test_case2)

results = await suite.run_all()
summary = suite.get_summary()
```

## 📊 Результаты тестирования

### Формат результатов

```python
{
    "test_name": "basic_response_test",
    "success": True,
    "duration": 0.45,
    "step_results": [...],
    "warnings": []
}
```

### Сводка по категориям

```
📊 СВОДКА РЕЗУЛЬТАТОВ ТЕСТИРОВАНИЯ
============================================================
✅ AGENTS           | Пройдено:   5 | Ошибок:   0 | Время:   2.34s
✅ TOOLS            | Пройдено:   8 | Ошибок:   0 | Время:   1.87s
✅ CONTEXT          | Пройдено:   6 | Ошибок:   0 | Время:   0.92s
✅ INTEGRATION      | Пройдено:   3 | Ошибок:   0 | Время:   4.12s
------------------------------------------------------------
✅ ОБЩИЙ ИТОГ:     | Пройдено:  22 | Ошибок:   0 | Время:   9.25s
📈 Успешность: 100.0%
```

## 🔧 Создание новых тестов

### 1. Тест агента

```python
@pytest.mark.asyncio
async def test_new_agent_feature():
    async with TestEnvironment() as env:
        env.set_mock_responses(["Expected response"])
        
        response = await env.agent_factory.run_agent(
            "agent_key",
            "Test message"
        )
        
        assert response is not None
        assert "expected_content" in response.lower()
```

### 2. Тест инструмента

```python
@pytest.mark.asyncio
async def test_new_tool():
    from tools.new_tool import new_function
    
    result = await new_function("test_input")
    
    assert result is not None
    assert "success" in result.lower()
```

### 3. Интеграционный тест

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_complex_workflow():
    async with TestEnvironment() as env:
        # Подготовка данных
        env.create_test_file("input.txt", "test data")
        
        # Мультиступенчатый процесс
        responses = [
            "Step 1 complete",
            "Step 2 complete", 
            "Workflow finished"
        ]
        env.set_mock_responses(responses)
        
        # Выполнение
        final_result = await env.agent_factory.run_agent(
            "test_full_agent",
            "Execute complex workflow"
        )
        
        # Проверки
        assert "finished" in final_result.lower()
```

## 🎯 Лучшие практики

### 1. Изоляция тестов
- Используйте `TestEnvironment` для изоляции
- Очищайте временные файлы
- Не полагайтесь на внешние сервисы в unit-тестах

### 2. Мокирование
- Мокайте внешние API вызовы
- Используйте предсказуемые ответы
- Тестируйте различные сценарии ответов

### 3. Читаемость
- Используйте описательные имена тестов
- Добавляйте комментарии к сложным тестам
- Группируйте связанные тесты в классы

### 4. Производительность
- Используйте `pytest.mark.asyncio` для асинхронных тестов
- Кешируйте тяжелые операции setup
- Запускайте медленные тесты с маркером `@pytest.mark.slow`

## 🚨 Отладка тестов

### Включение детального логирования

```python
# В тестовой конфигурации
settings:
  debug: true
  agent_logging:
    enabled: true
    level: "full"
```

### Проверка состояния тестов

```python
# Проверка контекста
context_info = env.agent_factory.get_context_info()
print(f"Messages: {context_info['message_count']}")

# Проверка выполнений
executions = env.agent_factory.get_recent_executions(5)
for exec in executions:
    print(f"Agent: {exec.agent_name}, Status: {exec.error or 'OK'}")

# Проверка моков
mock_calls = env.get_mock_calls()
print(f"Mock calls: {len(mock_calls)}")
```

### Общие проблемы

1. **Тест зависает**: Проверьте таймауты в конфигурации
2. **Агент не создается**: Проверьте наличие модели в тестовой конфигурации
3. **Инструмент не найден**: Убедитесь, что инструмент определен в тестовой конфигурации
4. **Контекст не сохраняется**: Проверьте настройки истории в конфигурации

## 📈 Метрики и покрытие

### Измерение покрытия

```bash
# Установка coverage
pip install coverage

# Запуск с измерением покрытия
coverage run -m pytest tests/
coverage report -m
coverage html  # HTML отчет
```

### Метрики производительности

- **Время создания агента**: < 1 секунды
- **Время ответа агента**: < 5 секунд (с моками)
- **Память**: Стабильное использование при множественных запусках
- **Параллелизм**: Поддержка 10+ одновременных агентов

## 🔄 CI/CD интеграция

### GitHub Actions

```yaml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - name: Set up Python
        uses: actions/setup-python@v2
        with:
          python-version: 3.9
      
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
          pip install pytest pytest-asyncio
      
      - name: Run tests
        run: python tests/run_tests.py
```

### Локальные хуки

```bash
# pre-commit хук
#!/bin/sh
python tests/run_tests.py --type unit
if [ $? -ne 0 ]; then
    echo "Тесты не прошли. Коммит отменен."
    exit 1
fi
```