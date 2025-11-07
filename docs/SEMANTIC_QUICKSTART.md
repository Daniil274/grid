# Быстрый старт: Семантический поиск

Руководство по быстрой настройке и использованию семантического поиска в Grid.

## Установка

### 1. Установить зависимости

```bash
# Опция 1: Установить все зависимости
pip install sentence-transformers chromadb

# Опция 2: Использовать requirements файл
pip install -r requirements-semantic.txt
```

### 2. Проверить установку

```python
python -c "from core.embeddings import EmbeddingsManager; print('Embeddings доступны!')"
```

Если видите "Embeddings доступны!" - всё готово!

## Быстрый пример

### Пример 1: Поиск по коду (через Python)

```python
from core.embeddings import CodeSearchManager
from pathlib import Path

# Создать менеджер
search = CodeSearchManager()

# Проиндексировать файлы
files = list(Path("src").glob("**/*.py"))
search.add_code_files(files, base_path=Path("src"))

# Искать
results = search.search_code("authentication logic", n_results=5)

for r in results:
    print(f"\nFile: {r['metadata']['file_path']}")
    print(f"Relevance: {r['similarity']:.2%}")
    print(f"Code:\n{r['text'][:200]}...")
```

### Пример 2: Поиск по коду (через агента)

1. **Создать конфигурацию** (`config.yaml`):

```yaml
tools:
  semantic_search:
    type: "function"
    name: "semantic_search"
    description: "Семантический поиск по коду"

  index_codebase:
    type: "function"
    name: "index_codebase"
    description: "Индексация кодовой базы"

agents:
  code_agent:
    name: "Code Expert"
    model: "gpt4"
    tools:
      - "semantic_search"
      - "index_codebase"
      - "file_read"
```

2. **Использовать агента**:

```python
from core.agent_factory import AgentFactory

factory = AgentFactory()

# Проиндексировать код
response = await factory.run_agent(
    agent_key="code_agent",
    message="Проиндексируй код в директории src с расширениями py,js"
)

# Искать
response = await factory.run_agent(
    agent_key="code_agent",
    message="Найди все места, где обрабатываются ошибки"
)
```

### Пример 3: Семантический поиск по истории диалогов

```python
from core.context import ContextManager

# Создать контекст с embeddings
context = ContextManager(enable_embeddings=True)

# Добавить сообщения
context.add_message("user", "Как реализовать JWT аутентификацию?")
context.add_message("assistant", "Используй библиотеку PyJWT...")

context.add_message("user", "Как подключиться к базе данных?")
context.add_message("assistant", "Используй SQLAlchemy...")

# Поиск по истории
results = context.semantic_search_history(
    query="authentication",
    n_results=3
)

for r in results:
    print(f"Relevance: {r['similarity']:.2%}")
    print(f"{r['metadata']['role']}: {r['text']}\n")
```

## Типичные сценарии использования

### Сценарий 1: "Где у нас обрабатываются ошибки?"

```python
# Традиционный способ (grep)
grep -r "try:" src/
grep -r "except" src/
grep -r "raise" src/

# Семантический поиск (один запрос)
results = search.search_code("error handling and exception management")
# Найдёт: try/except, raise, custom exceptions, error handlers, logging errors
```

### Сценарий 2: "Найти все API endpoints"

```python
results = search.search_code("REST API endpoints HTTP routes")
# Найдёт:
# - @app.route() decorators
# - FastAPI path operations
# - API view classes
# - URL patterns
```

### Сценарий 3: "Как мы работали с подобной задачей раньше?"

```python
# Поиск по истории диалогов
results = context.semantic_search_history(
    query="implementing database migrations",
    n_results=5
)
# Найдёт все обсуждения про миграции, схемы БД, Alembic и т.д.
```

### Сценарий 4: RAG для агента

```python
# Агент автоматически получит релевантный контекст
context = ContextManager(enable_embeddings=True)

# Добавить много информации
context.add_message("user", "Explain our authentication system")
context.add_message("assistant", "We use JWT with refresh tokens...")
# ... ещё 100 сообщений

# Через месяц работы
relevant = context.get_relevant_context_semantic(
    query="How do we handle user sessions?",
    max_results=5
)
# Найдёт только релевантные сообщения про сессии из всей истории
```

## Советы по эффективному использованию

### ✅ Хорошие запросы

```python
# Конкретные и описательные
search.search_code("async database connection pooling")
search.search_code("user authentication with JWT tokens")
search.search_code("error handling with custom exceptions")

# С контекстом
search.search_code("React component for user profile form")
search.search_code("Python function for data validation")
```

### ❌ Плохие запросы

```python
# Слишком общие
search.search_code("code")
search.search_code("function")

# Слишком короткие
search.search_code("db")
search.search_code("api")
```

### Фильтры для точности

```python
# Поиск только в Python файлах
results = search.search_code(
    query="database models",
    file_extension=".py"
)

# Поиск только в директории
results = search.search_code(
    query="API routes",
    directory="src/api"
)
```

## Производительность

### Первая индексация

```python
# ~100 файлов: ~5-10 секунд
# ~1000 файлов: ~30-60 секунд
# ~10000 файлов: ~5-10 минут
```

### Поиск

```python
# Топ 5 результатов: <100ms
# Топ 50 результатов: <500ms
```

### Память

```python
# Модель: ~200MB
# Индекс: ~1MB на 1000 документов
# Общее: ~250-500MB для типичного проекта
```

## Устранение проблем

### Проблема: "ImportError: No module named 'sentence_transformers'"

```bash
# Решение:
pip install sentence-transformers chromadb
```

### Проблема: Медленная первая индексация

```python
# Решение: Используйте батчевую обработку (уже реализовано)
# Или индексируйте меньше файлов:
files = list(Path("src").glob("**/*.py"))[:100]  # Только первые 100
```

### Проблема: Низкая релевантность результатов

```python
# Решение 1: Улучшите запрос
# Плохо:
results = search.search_code("auth")

# Хорошо:
results = search.search_code("user authentication with JWT tokens")

# Решение 2: Используйте фильтры
results = search.search_code(
    query="authentication",
    file_extension=".py"
)

# Решение 3: Проверяйте similarity score
for r in results:
    if r['similarity'] > 0.5:  # Только высокорелевантные
        process(r)
```

### Проблема: Большое потребление памяти

```python
# Решение: Используйте persistent storage
manager = EmbeddingsManager(
    persist_directory="./embeddings_db"
)
# ChromaDB будет использовать диск вместо RAM
```

## Дальнейшие шаги

1. **Прочитать полную документацию**: [SEMANTIC_SEARCH.md](./SEMANTIC_SEARCH.md)
2. **Посмотреть примеры конфигурации**: [config-semantic-example.yaml](./config-semantic-example.yaml)
3. **Запустить тесты**: `pytest tests/test_embeddings.py -v`
4. **Экспериментировать**: Попробуйте разные запросы и фильтры

## Полезные ссылки

- [Sentence Transformers Documentation](https://www.sbert.net/)
- [ChromaDB Documentation](https://docs.trychroma.com/)
- [Grid Documentation](../README.md)

## Примеры использования в других проектах

### GitHub Copilot Style

```python
# Найти похожие функции для автодополнения
def authenticate_user(username, password):
    # Поиск похожих функций аутентификации
    results = search.search_code(
        "user authentication login validation",
        n_results=3
    )
    # Использовать как примеры для имплементации
```

### Code Review Assistant

```python
# Найти примеры хороших практик
results = search.search_code(
    "error handling best practices with logging",
    n_results=5
)
# Сравнить с текущим кодом
```

### Documentation Helper

```python
# Найти код для документирования
results = search.search_code(
    "API endpoint handlers",
    n_results=10
)
# Автоматически генерировать документацию
```
