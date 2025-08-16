# Система тестирования Grid

Комплексная система тестирования для платформы агентов Grid, включающая модульные, интеграционные и функциональные тесты.

## 🆕 Новые возможности

### Автоматизированное измерение покрытия
- Интегрированная поддержка coverage
- Автоматическая генерация HTML, XML, JSON отчетов
- Настраиваемые пороги покрытия
- Визуализация непокрытых участков кода

### Расширенные security тесты
- Тесты для SecurityGuardian
- Проверка ContextQualityAgent  
- Анализ TaskAnalyzer
- Интеграционные security сценарии

### Комплексные API тесты
- Полное покрытие OpenAI-compatible endpoints
- Тесты middleware компонентов
- Performance и stress тестирование
- Интеграционные API сценарии

## 🗂️ Структура тестов

```
tests/
├── conftest.py                    # Общие фикстуры и настройки pytest
├── coverage_runner.py             # Автоматизация измерения покрытия
├── config_test.yaml              # Тестовая конфигурация агентов
├── config_qa.yaml               # QA конфигурация для реальных тестов
├── 
├── test_framework.py             # Базовый фреймворк тестирования
├── enhanced_test_framework.py    # Расширенный фреймворк с метриками
├── 
├── test_agents.py               # Тесты агентов
├── test_tools.py                # Тесты инструментов
├── test_context.py              # Тесты системы контекста
├── test_enhanced_features.py    # Тесты расширенных возможностей
├── 
├── test_security.py             # 🆕 Security тесты
├── test_api_comprehensive.py    # 🆕 Комплексные API тесты
├── test_integration.py          # Интеграционные тесты
├── test_integration_lmstudio.py # Интеграция с LMStudio
├── test_openai_compat.py        # OpenAI совместимость
├── 
├── run_tests.py                 # Основной запускающий скрипт
├── qa_test_runner.py            # QA тестирование на реальных моделях
├── mock_tools.py                # Моки для тестирования
└── README.md                    # Данная документация
```

## 🚀 Быстрый старт

### Установка зависимостей

```bash
# Установка основных зависимостей Grid
pip install -r requirements.txt

# Установка зависимостей для тестирования
pip install pytest pytest-asyncio coverage fastapi httpx
```

### Запуск всех тестов

```bash
# Основной способ - через coverage runner
python tests/coverage_runner.py

# Альтернативные способы
python tests/run_tests.py
pytest tests/ -v

# С измерением покрытия
coverage run -m pytest tests/
coverage report -m
```

### Запуск конкретных категорий тестов

```bash
# 🆕 Использование маркеров pytest
pytest -m "unit"              # Только unit тесты
pytest -m "integration"       # Интеграционные тесты
pytest -m "security"          # Security тесты
pytest -m "api"               # API тесты
pytest -m "not slow"          # Исключить медленные тесты

# Через coverage runner
python tests/coverage_runner.py --markers "unit"
python tests/coverage_runner.py --markers "security"

# Конкретные файлы
python tests/coverage_runner.py --pattern "tests/test_security.py"
```

## 📋 Типы тестов

### 1. Unit тесты
Быстрые изолированные тесты основных компонентов:
- `test_agents.py` - Тесты агентов
- `test_tools.py` - Тесты инструментов  
- `test_context.py` - Тесты контекста
- `test_enhanced_features.py` - Расширенные возможности

### 2. 🆕 Security тесты (`test_security.py`)
Проверяют безопасность системы:
- **SecurityGuardian**: Валидация входа/выхода
- **ContextQualityAgent**: Анализ качества контекста
- **TaskAnalyzer**: Оценка сложности и рисков
- **SecurityAwareFactory**: Безопасное создание агентов

```python
@pytest.mark.security
@pytest.mark.asyncio
async def test_security_guardian_creation():
    async with AgentTestEnvironment() as env:
        guardian = SecurityGuardian(env.config)
        assert guardian is not None
```

### 3. 🆕 API тесты (`test_api_comprehensive.py`)
Полное тестирование API функциональности:
- **OpenAI Compatible**: Chat/Completions endpoints
- **Streaming**: Потоковые ответы
- **Agent Management**: CRUD операции с агентами
- **System API**: Health checks и системная информация
- **Middleware**: Authentication, Rate limiting, Security
- **Performance**: Нагрузочное тестирование

```python
@pytest.mark.api
def test_chat_completions_endpoint():
    with patch('core.agent_factory.AgentFactory') as mock_factory:
        response = client.post("/v1/chat/completions", json={
            "model": "test_simple_agent",
            "messages": [{"role": "user", "content": "Привет"}]
        })
        assert response.status_code == 200
```

### 4. Integration тесты
Проверяют взаимодействие компонентов:
- End-to-end рабочие процессы
- Интеграция с внешними сервисами
- Реальные сценарии использования

## 🆕 Автоматизированное измерение покрытия

### Coverage Runner

Новый инструмент `coverage_runner.py` автоматизирует анализ покрытия:

```bash
# Базовое использование
python tests/coverage_runner.py

# С дополнительными опциями
python tests/coverage_runner.py \
    --pattern "tests/test_agents.py" \
    --markers "unit" \
    --format "html" \
    --verbose

# Только генерация отчетов (без запуска тестов)
python tests/coverage_runner.py --no-tests --format "all"

# Установка зависимостей
python tests/coverage_runner.py --install-deps
```

### Форматы отчетов
- `terminal` - Текстовый отчет в консоли
- `html` - Интерактивный HTML отчет
- `xml` - XML для CI/CD интеграции  
- `json` - JSON для программной обработки
- `all` - Все форматы одновременно

### Интерпретация результатов

```
📈 СВОДКА ПОКРЫТИЯ КОДА
============================================================
🎉 Общее покрытие: 92% - ОТЛИЧНО
✅ core/: 95% покрытие
⚠️ security_agents/: 78% покрытие (НУЖНО УЛУЧШИТЬ)
```

## ⚙️ Конфигурация тестов

### 🆕 Улучшенная конфигурация pytest (`pytest.ini`)

```ini
[pytest]
addopts = -q --tb=short --strict-markers
markers =
    unit: marks tests as unit tests
    integration: marks tests as integration tests  
    security: marks tests as security-related
    api: marks tests as API tests
    performance: marks tests as performance tests
    slow: marks tests as slow running

# Coverage configuration
[coverage:run]
source = .
omit = tests/*, venv/*, .venv/*

[coverage:report]
show_missing = true
precision = 2
exclude_lines =
    pragma: no cover
    def __repr__
    raise NotImplementedError
```

### 🆕 Общие фикстуры (`conftest.py`)

Централизованные фикстуры для всех тестов:

```python
@pytest.fixture
async def test_environment():
    """Изолированная тестовая среда."""
    async with AgentTestEnvironment() as env:
        yield env

@pytest.fixture  
def mock_openai_client():
    """Мок OpenAI клиента."""
    # Автоматическое мокирование API
```

## 🔧 Создание новых тестов

### 1. Security тест

```python
@pytest.mark.security
@pytest.mark.asyncio
async def test_input_validation():
    async with AgentTestEnvironment() as env:
        guardian = SecurityGuardian(env.config)
        
        # Безопасный ввод
        safe_input = "Привет, как дела?"
        assert await guardian.validate_input(safe_input) is True
        
        # Опасный ввод
        dangerous_input = "rm -rf /"
        assert await guardian.validate_input(dangerous_input) is False
```

### 2. API тест

```python
@pytest.mark.api
def test_new_api_endpoint():
    client = TestClient(app)
    response = client.get("/new/endpoint")
    
    assert response.status_code == 200
    assert response.json()["status"] == "success"
```

### 3. Performance тест

```python
@pytest.mark.performance
def test_concurrent_requests():
    import threading
    
    results = []
    def make_request():
        response = client.get("/system/health")
        results.append(response.status_code)
    
    threads = [threading.Thread(target=make_request) for _ in range(10)]
    for t in threads: t.start()
    for t in threads: t.join()
    
    assert all(status == 200 for status in results)
```

## 🎯 Лучшие практики

### 1. 🆕 Использование маркеров
```python
@pytest.mark.unit          # Быстрые unit тесты
@pytest.mark.integration   # Медленные integration тесты  
@pytest.mark.security      # Security-related тесты
@pytest.mark.api           # API тесты
@pytest.mark.performance   # Performance тесты
@pytest.mark.slow          # Медленные тесты
```

### 2. Фикстуры
```python
# Используйте общие фикстуры из conftest.py
async def test_with_environment(test_environment):
    # test_environment автоматически создается и очищается
    response = await test_environment.agent_factory.run_agent(...)
```

### 3. Мокирование
```python
# Используйте готовые моки
def test_with_mock_client(mock_openai_client):
    # Клиент уже замокан
    mock_openai_client.chat.completions.create.return_value = {...}
```

## 📊 Анализ покрытия

### Пороги покрытия
- **90%+**: Отлично 🎉
- **80-89%**: Хорошо ✅  
- **70-79%**: Удовлетворительно ⚠️
- **<70%**: Нужно улучшить ❌

### Критические компоненты (цель 90%+)
- `core/` - Основная логика
- `api/` - API endpoints
- `security_agents/` - Security компоненты

### Менее критичные (цель 70%+)
- `utils/` - Вспомогательные утилиты
- `tools/` - Инструменты

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
          pip install pytest pytest-asyncio coverage
      
      - name: Run tests with coverage
        run: |
          python tests/coverage_runner.py --format xml
      
      - name: Upload coverage to Codecov
        uses: codecov/codecov-action@v1
```

### Локальные хуки

```bash
# pre-commit хук
#!/bin/sh
python tests/coverage_runner.py --markers "unit" --format terminal
if [ $? -ne 0 ]; then
    echo "❌ Unit тесты не прошли. Коммит отменен."
    exit 1
fi
```

## 🚨 Отладка тестов

### Запуск с детальным выводом

```bash
# Подробный вывод
python tests/coverage_runner.py --verbose

# Pytest с детальным выводом
pytest tests/ -v -s

# Конкретный тест с отладкой
pytest tests/test_security.py::TestSecurityGuardian::test_input_validation -v -s
```

### Анализ покрытия

```bash
# Только генерация отчетов
python tests/coverage_runner.py --no-tests --format html

# Детальный анализ непокрытых строк
coverage report -m --skip-covered
```

### Профилирование тестов

```bash
# Медленные тесты
pytest --durations=10

# Только быстрые тесты
pytest -m "not slow"
```

## 📈 Метрики и мониторинг

### Автоматические метрики
- Время выполнения тестов
- Покрытие по компонентам
- Количество failed/passed тестов
- Анализ тестовых трендов

### Отчеты
- HTML dashboard с интерактивными графиками
- XML для интеграции с CI/CD
- JSON для программного анализа

## 🎉 Заключение

Система тестирования Grid теперь включает:

✅ **Исправлены предупреждения pytest**  
✅ **Автоматизированное измерение покрытия**  
✅ **Security тесты для всех компонентов**  
✅ **Комплексные API тесты**  
✅ **Улучшенная структура и организация**  
✅ **Централизованные фикстуры и конфигурация**  
✅ **Подробная документация**  

Используйте `python tests/coverage_runner.py` для запуска тестов с автоматическим анализом покрытия!