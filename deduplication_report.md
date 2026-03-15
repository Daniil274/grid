# Отчет о реализации дедупликации памяти в Grid

## ✅ ЗАДАЧА ВЫПОЛНЕНА

Дедупликация при сохранении памяти полностью реализована и протестирована.

---

## Список измененных файлов

### 1. `grid/core/memory_store.py`

**Добавленные импорты:**
```python
from typing import ... Tuple
from difflib import SequenceMatcher
```

**Новые константы класса:**
```python
DEFAULT_SIMILARITY_THRESHOLD = 0.8  # 80% = дубликат
MAX_FTS_CANDIDATES = 50  # Макс. кандидатов от FTS5
IMPORTANCE_BOOST_ON_UPDATE = 0.1  # Увеличение importance при обновлении
```

**Новые методы:**

| Метод | Описание | Сигнатура |
|-------|----------|-----------|
| `_calculate_similarity()` | Вычисляет коэффициент схожести текстов через SequenceMatcher | `(text1, text2) → float` |
| `_fts_search_candidates()` | Быстрый FTS5 поиск кандидатов | `(query, max_candidates, type, user_id, agent_id, exclude_ids) → List[MemoryEntry]` |
| `find_similar()` | Поиск похожих записей с вычислением similarity | `(query, threshold=0.8, limit=5, ...) → List[Tuple[MemoryEntry, float]]` |
| `get_by_id()` | Получить запись по ID (для тестов) | `(entry_id) → MemoryEntry` |

**Модифицированные методы:**

| Метод | Изменения |
|-------|-----------|
| `save()` | Добавлены параметры `update_if_exists=False` и `similarity_threshold=None` |

---

### 2. `grid/tests/test_memory_deduplication.py` (новый файл)

**Структура тестов: 37 тестов**

| Класс тестов | Количество | Описание |
|--------------|------------|----------|
| `TestCalculateSimilarity` | 10 | Граничные значения для `_calculate_similarity()` |
| `TestFindSimilar` | 9 | Поиск похожих записей через `find_similar()` |
| `TestSaveDeduplication` | 9 | Дедупликация при `save(update_if_exists=True)` |
| `TestEdgeCases` | 6 | Граничные случаи (пустой запрос, спецсимволы, Unicode) |
| `TestIntegration` | 3 | Интеграционные тесты полного сценария |

**Результат запуска:**
```
pytest tests/test_memory_deduplication.py -v
============================= test session starts ==============================
collected 37 items

tests/test_memory_deduplication.py ..................................... [100%]

============================== 37 passed in 4.54s =============================
```

---

## Примеры использования новых методов

### Пример 1: Автоматическая дедупликация при save()

```python
from core.memory_store import MemoryStore

store = MemoryStore("data/memory.db")

# Первое сохранение — создаёт новую запись
id1 = store.save(
    content="User prefers Python for backend development",
    type="long_term",
    tags="preference,programming",
    update_if_exists=True  # Включена дедупликация
)
# → id1 = 1 (новая запись)

# Повторное сохранение похожего контента — обновляет существующую
id2 = store.save(
    content="User prefers Python for backend",  # Похоже на 85%
    type="long_term",
    tags="preference,programming",
    update_if_exists=True
)
# → id2 = 1 (та же запись, обновлена)
# Лог: 🔄 Found similar entry #1 (similarity=0.85), updating...
```

### Пример 2: Поиск похожих записей

```python
# Найти все записи с схожестью > 70%
similar = store.find_similar(
    query="Grid Agent System architecture",
    threshold=0.7,
    limit=10
)

for entry, score in similar:
    print(f"[{score:.2%}] #{entry.id}: {entry.content[:50]}...")
```

**Вывод:**
```
[95.3%] #15: Grid Agent Framework architecture designed for...
[87.1%] #8: The architecture of the Grid agent system includes...
[72.4%] #22: Agent system with modular architecture...
```

### Пример 3: Проверка перед сохранением (ручной режим)

```python
# Ручная проверка перед сохранением
content = "New memory entry about Python"
similar = store.find_similar(content, threshold=0.8, limit=1)

if similar:
    existing_id, score = similar[0]
    print(f"⚠️ Найден дубликат: #{existing_id} (similarity={score:.2%})")
    # Решение: обновить или пропустить
else:
    store.save(content, type="long_term")
```

### Пример 4: Фильтрация по user_id и agent_id

```python
# Дедупликация только для конкретного пользователя
store.save(
    content="User likes coffee",
    type="long_term",
    user_id="user_123",
    agent_id="agent_456",
    update_if_exists=True
)
# Найдёт только записи этого пользователя и агента
```

### Пример 5: Кастомный порог схожести

```python
# Более строгий порог (90% вместо 80%)
store.save(
    content="Almost identical content",
    type="long_term",
    update_if_exists=True,
    similarity_threshold=0.9  # Только очень похожие считаются дубликатами
)
```

### Пример 6: Исключение записей из поиска

```python
# Найти похожие, исключая конкретные ID
similar = store.find_similar(
    query="Some content",
    threshold=0.8,
    exclude_ids=[1, 2, 3]  # Исключить эти записи из поиска
)
```

### Пример 7: Получение записи по ID

```python
# Получить запись по ID (новый метод)
entry = store.get_by_id(42)
if entry:
    print(f"#{entry.id}: {entry.content}")
    print(f"Importance: {entry.importance}")
    print(f"Tags: {entry.tags}")
```

---

## Алгоритм дедупликации

### 1. Двухэтапный поиск

**Шаг 1: Быстрый FTS5 (отбор кандидатов)**
- Извлекает значимые слова из запроса (длина > 3 символа)
- Строит FTS5 OR-запрос для поиска кандидатов
- Возвращает до 50 кандидатов

**Шаг 2: Точный SequenceMatcher (вычисление similarity)**
- Для каждого кандидата вычисляет `SequenceMatcher.ratio()`
- Нормализует тексты: lowercase, удаление лишних пробелов
- Фильтрует по threshold (>= 0.8 = дубликат)

### 2. Стратегия обновления

При обнаружении дубликата:
1. Увеличивает `importance` на 0.1 (max 1.0)
2. Обновляет `content` новым значением
3. Обновляет `updated_at` автоматически
4. Логирует: `🔄 Found similar entry #{id} (similarity=X.XX), updating...`

---

## Технические детали

### Backward Compatibility

Все новые параметры опциональные:
- `update_if_exists=False` (по умолчанию)
- `similarity_threshold=None` (использует `DEFAULT_SIMILARITY_THRESHOLD`)

Существующий код работает без изменений.

### Фильтрация при дедупликации

Дедупликация учитывает:
- **type**: записи разных типов не считаются дубликатами
- **user_id**: разные пользователи могут иметь одинаковый контент
- **agent_id**: разные агенты изолированы

### Производительность

- FTS5 для отбора кандидатов: O(log N)
- SequenceMatcher для exact similarity: O(M*N) для каждой пары
- Оптимизация: только для кандидатов (max 50), не для всей базы

---

## Рекомендации по использованию

### Когда использовать `update_if_exists=True`

✅ **Подходит для:**
- Фактов о пользователе (предпочтения, настройки)
- Долгосрочных знаний (long_term)
- Повторяющихся данных от одного источника

❌ **Не подходит для:**
- Исторических событий (каждое событие уникально)
- Логики задач (task, task_plan)
- Когда важно сохранять каждую версию

### Настройка порога

| Порог | Когда использовать |
|-------|-------------------|
| 0.9+ | Только точные совпадения |
| 0.8 | Стандартный (рекомендуется) |
| 0.7-0.75 | Более агрессивная дедупликация |

### Мониторинг

Логирование включает:
- `🔄` — найдено совпадение, запись обновлена
- `💾` — создана новая запись (стандартное поведение)

Проверяйте логи для отладки дедупликации.

---

## Заключение

Дедупликация памяти в Grid полностью реализована:
- ✅ Метод `find_similar()` для поиска похожих записей
- ✅ Параметр `update_if_exists=True` в `save()`
- ✅ Алгоритм similarity на основе SequenceMatcher
- ✅ 37 тестов покрывают все сценарии
- ✅ Backward compatibility сохранена

**Результат:** Исключены дублирующие факты в памяти при сохранении.
