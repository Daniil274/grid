# Семантический поиск и Embeddings

Grid теперь поддерживает семантический поиск для улучшения работы агентов с кодовой базой и контекстом диалогов.

## Возможности

### 1. Семантический поиск по кодовой базе

Ищите код по смыслу, а не по точному совпадению слов:

```python
# Вместо точного совпадения:
search_files("function definition")  # Найдёт только "function definition"

# Семантический поиск найдёт:
semantic_search_code("function definition")
# - "def calculate_sum()"
# - "class DataProcessor"
# - "async def handle_request()"
```

### 2. Умный контекст для агентов (RAG)

ContextManager теперь может искать релевантные части истории диалогов вместо просто последних N сообщений:

```python
# Традиционный подход - берёт последние 5 сообщений
context.get_conversation_context(last_n=5)

# Семантический подход - находит релевантные сообщения
context.get_relevant_context_semantic(
    query="как обрабатывать ошибки?",
    max_results=5
)
```

### 3. Поиск похожих задач из прошлого

```python
# Поиск по истории диалогов
results = context.semantic_search_history(
    query="обработка MCP серверов",
    n_results=5
)
```

## Установка

### Основные зависимости

```bash
pip install sentence-transformers chromadb
```

Или установите с опциональными зависимостями для семантического поиска:

```bash
pip install -e ".[semantic]"
```

### Минимальные требования

- Python 3.8+
- ~200MB для модели embeddings (all-MiniLM-L6-v2)
- ~50MB RAM при работе

## Использование

### Инструменты для агентов

Добавьте семантические инструменты в конфигурацию агента:

```yaml
# config.yaml

tools:
  semantic_search:
    type: "function"
    name: "semantic_search"
    description: "Семантический поиск по кодовой базе"
    prompt_addition: |
      Используй semantic_search(query) для поиска кода по смыслу.
      Примеры запросов:
      - "error handling" → найдёт try/except блоки
      - "database queries" → найдёт SQL и ORM код
      - "API endpoints" → найдёт роуты и обработчики

  index_codebase:
    type: "function"
    name: "index_codebase"
    description: "Индексация кодовой базы для быстрого поиска"
    prompt_addition: |
      Используй index_codebase() для создания индекса кода перед поиском.
      Нужно делать один раз или при изменении кода.

agents:
  code_agent:
    name: "Code Agent"
    model: "gpt4"
    tools:
      - "semantic_search"
      - "index_codebase"
      - "file_read"
      - "file_write"
```

### Программное использование

#### Семантический поиск по коду

```python
from core.embeddings import CodeSearchManager
from pathlib import Path

# Создать менеджер поиска
search = CodeSearchManager()

# Проиндексировать файлы
files = list(Path("src").glob("**/*.py"))
search.add_code_files(files, base_path=Path("src"))

# Поиск
results = search.search_code(
    query="authentication logic",
    n_results=5,
    file_extension=".py"
)

for result in results:
    print(f"File: {result['metadata']['file_path']}")
    print(f"Relevance: {result['similarity']:.2%}")
    print(f"Code:\n{result['text']}\n")
```

#### Семантический поиск по истории

```python
from core.context import ContextManager

# Создать контекст с embeddings
context = ContextManager(
    max_history=50,
    enable_embeddings=True
)

# Добавить сообщения
context.add_message("user", "Как реализовать аутентификацию?")
context.add_message("assistant", "Используй JWT токены...")

# Семантический поиск
results = context.semantic_search_history(
    query="authentication implementation",
    n_results=3
)

for result in results:
    print(f"Role: {result['metadata']['role']}")
    print(f"Relevance: {result['similarity']:.2%}")
    print(f"Message: {result['text']}\n")
```

#### RAG для агентов

```python
# Получить релевантный контекст для задачи
relevant_context = context.get_relevant_context_semantic(
    query="implement user login",
    max_results=5,
    include_tools=True
)

# Передать агенту как дополнительный контекст
agent_response = await factory.run_agent(
    agent_key="code_agent",
    message=f"{relevant_context}\n\nTask: Implement user login"
)
```

## Примеры использования

### Пример 1: Поиск всех обработчиков ошибок

```python
# Вместо regex или grep
results = search.search_code(
    query="error handling exception management",
    n_results=10
)

# Найдёт:
# - try/except блоки
# - error handlers
# - exception classes
# - logging errors
```

### Пример 2: Поиск API endpoints

```python
results = search.search_code(
    query="REST API endpoints HTTP routes",
    n_results=10,
    file_extension=".py"
)

# Найдёт:
# - @app.route() декораторы
# - FastAPI endpoints
# - API handlers
```

### Пример 3: Контекстный агент

```yaml
agents:
  smart_agent:
    name: "Smart Context Agent"
    model: "gpt4"
    tools:
      - "semantic_search"
    # Агент автоматически использует семантический контекст
```

## Архитектура

```
┌─────────────────────────────────────────┐
│           Grid Agent System              │
├─────────────────────────────────────────┤
│                                          │
│  ┌────────────────────────────────┐     │
│  │   ContextManager                │     │
│  │   - История диалогов            │     │
│  │   - Семантический поиск         │     │
│  │   - RAG для агентов             │     │
│  └────────────────────────────────┘     │
│           ↓                              │
│  ┌────────────────────────────────┐     │
│  │   EmbeddingsManager             │     │
│  │   - sentence-transformers       │     │
│  │   - all-MiniLM-L6-v2 model      │     │
│  │   - Кэширование embeddings      │     │
│  └────────────────────────────────┘     │
│           ↓                              │
│  ┌────────────────────────────────┐     │
│  │   ChromaDB                      │     │
│  │   - Vector storage              │     │
│  │   - Similarity search           │     │
│  │   - Metadata filtering          │     │
│  └────────────────────────────────┘     │
│                                          │
└─────────────────────────────────────────┘
```

## Технические детали

### Модель Embeddings

- **Модель**: `all-MiniLM-L6-v2` от sentence-transformers
- **Размер**: ~80MB
- **Размерность**: 384 dimensions
- **Скорость**: ~500 sentences/sec на CPU
- **Качество**: State-of-the-art для семантического поиска

### Векторное хранилище

- **База данных**: ChromaDB
- **Режимы**: In-memory или persistent
- **Индексация**: HNSW для быстрого поиска
- **Фильтрация**: По метаданным (role, file_type, timestamp)

### Производительность

- **Индексация**: ~100 файлов/сек
- **Поиск**: <100ms для топ-10 результатов
- **Память**: ~50MB + размер индекса (~1MB на 1000 документов)

## Настройка

### Конфигурация ContextManager

```python
context = ContextManager(
    max_history=50,
    enable_embeddings=True,
    embeddings_persist_path="./data/embeddings"  # Опционально
)
```

### Конфигурация модели

```python
from core.embeddings import EmbeddingsManager

# Использовать другую модель
manager = EmbeddingsManager(
    model_name="paraphrase-multilingual-MiniLM-L12-v2",  # Для русского языка
    persist_directory="./embeddings_db"
)
```

### Чанкинг кода

```python
# Настроить размер чанков для больших файлов
search = CodeSearchManager()
search.add_code_files(
    files,
    chunk_size=1500  # Символов на чанк
)
```

## Лучшие практики

### 1. Индексация

- Индексируйте код один раз при запуске
- Переиндексируйте только при изменениях
- Используйте фильтры по расширениям файлов

```python
# Плохо: индексация каждый раз
search.search_code(query, reindex=True)

# Хорошо: индексация один раз
search.add_code_files(files)  # Один раз
search.search_code(query)      # Много раз
```

### 2. Формулировка запросов

```python
# Плохо: слишком общий запрос
search.search_code("code")

# Хорошо: конкретный запрос
search.search_code("database connection pooling")

# Отлично: запрос с контекстом
search.search_code("async database connection with retry logic")
```

### 3. Использование результатов

```python
# Проверяйте релевантность
results = search.search_code(query)
for r in results:
    if r['similarity'] > 0.5:  # Только релевантные
        process_result(r)
```

### 4. Кэширование

```python
# Embeddings автоматически кэшируются
# Не пересоздавайте менеджер без необходимости
manager = EmbeddingsManager()  # Создать один раз
# Использовать много раз
```

## Устранение проблем

### Медленная индексация

```python
# Используйте батчевую обработку
manager.embed_texts(texts)  # Быстрее чем:
for text in texts:
    manager.embed_text(text)  # Медленнее
```

### Большое потребление памяти

```python
# Используйте persistent storage вместо in-memory
manager = EmbeddingsManager(
    persist_directory="./embeddings"
)
```

### Низкая релевантность результатов

```python
# Улучшите запросы или используйте фильтры
results = search.search_code(
    query="more specific query",
    file_extension=".py"  # Фильтр по типу
)
```

## Roadmap

- [ ] Поддержка multilingual моделей для русского языка
- [ ] Гибридный поиск (semantic + keyword)
- [ ] Ранжирование результатов с учётом файловой структуры
- [ ] Кэширование результатов поиска
- [ ] API для управления индексами
- [ ] Incremental indexing (обновление только изменённых файлов)

## См. также

- [Context Management](./CONTEXT.md)
- [Agent Configuration](./CONFIGURATION.md)
- [Tools Guide](./TOOLS.md)
