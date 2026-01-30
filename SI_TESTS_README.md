# Social Intelligence Framework - Тестовые сценарии

## Обзор изменений

Реализованы все 8 пунктов из плана:
1. ✅ Blackboard context injection - агенты читают blackboard перед выполнением
2. ✅ Tools для critics/validators - могут использовать filesystem и др.
3. ✅ Новый примитив `revise()` - исправление по обратной связи
4. ✅ Cross-step context - каждый шаг видит всю историю
5. ✅ Iterative refinement в `_execute_pipeline()` - validate → revise → re-validate
6. ✅ Post-pipeline refinement в `orchestrate_emergent()` - финальная валидация с исправлением
7. ✅ Planner enhancement - планировщик видит blackboard и pipeline memory
8. ✅ Config updates - все промпты в YAML, новая секция refinement

## Структура тестов

### Тест 1: Simple Execute (`test_simple_execute.yaml`)
**Цель:** Проверить базовое чтение blackboard

**Что проверяется:**
- Агент читает blackboard перед выполнением
- Контекст из blackboard виден в промпте
- Записи сохраняются в `logs/test_simple_blackboard.json`

**Как запустить:**
```bash
python agent_chat.py -c test_simple_execute.yaml
```

**Что ожидать:**
- Первая задача выполняется "слепо"
- Вторая похожая задача видит контекст из первой
- В blackboard.json видны записи типа `ARTIFACT` от `executor-xxx`

---

### Тест 2: Critique → Revise (`test_critique_revise.yaml`)
**Цель:** Проверить цикл critique → revise

**Что проверяется:**
- Критик анализирует результат
- Reviser получает критику как feedback
- Исправленная версия лучше оригинала
- Tools передаются в critique и revise

**Как запустить:**
```bash
python agent_chat.py -c test_critique_revise.yaml
```

**Задача для теста:**
"Напиши простую функцию Python для сортировки списка"

**Что ожидать в `logs/test_critique_blackboard.json`:**
```json
[
  {
    "entry_type": "artifact",
    "author": "executor-abc123",
    "content": "def sort_list(lst): ..."
  },
  {
    "entry_type": "critique",
    "author": "critic-xyz456",
    "content": {
      "issues": ["Нет обработки пустого списка", "Нет type hints"],
      "severity": "minor",
      "overall_score": 0.6
    }
  },
  {
    "entry_type": "artifact",
    "author": "reviser-def789",
    "content": "def sort_list(lst: list) -> list:\n    if not lst:\n        return []\n    ..."
  }
]
```

---

### Тест 3: Validation Loop (`test_validation_loop.yaml`)
**Цель:** Проверить итеративный цикл validate → revise → re-validate

**Что проверяется:**
- Валидатор находит галлюцинации
- Автоматически запускается revise
- Повторная валидация проверяет исправление
- До 3 итераций (max_refinement_iterations=3)

**Как запустить:**
```bash
python agent_chat.py -c test_validation_loop.yaml
```

**Задача для теста:**
"Расскажи про фичи Python 4.0" (Python 4.0 не существует!)

**Что ожидать:**
1. Executor создаёт текст с галлюцинацией про Python 4.0
2. Validator находит: `{"valid": false, "issues": [{"type": "hallucination", "description": "Python 4.0 не существует"}]}`
3. Reviser исправляет: убирает упоминание Python 4.0
4. Re-validator проверяет: `{"valid": true}`

**В `logs/test_validation_blackboard.json`:**
- Несколько записей от validator с valid: false
- Записи от reviser с тегом "iteration_1", "iteration_2"
- Финальная запись от validator с valid: true

---

### Тест 4: Complex Pipeline (`test_complex_pipeline.yaml`)
**Цель:** Проверить branch → synthesize → validate

**Что проверяется:**
- Параллельное выполнение с разных перспектив (branch)
- Агенты видят работу друг друга через blackboard
- Synthesizer объединяет результаты
- Validator проверяет итоговый синтез
- Pipeline сохраняется в memory для переиспользования

**Как запустить:**
```bash
python agent_chat.py -c test_complex_pipeline.yaml
```

**Задача для теста:**
"Проанализируй файл core/primitives.py с точек зрения: security, performance, readability"

**Что ожидать:**
1. **Branch создаёт 3 агента:**
   - executor-security (анализ безопасности)
   - executor-performance (анализ производительности)
   - executor-readability (анализ читаемости)

2. **Каждый агент видит blackboard:**
   - security-agent постит свой анализ первым
   - performance-agent видит находки security-agent
   - readability-agent видит находки обоих предыдущих

3. **Synthesizer объединяет:**
   - Видит ВСЕ 3 анализа в blackboard
   - Создаёт единый отчёт
   - Устраняет противоречия

4. **Validator проверяет:**
   - Итоговый синтез на галлюцинации
   - Логическую консистентность

**В `logs/test_complex_blackboard.json` должно быть:**
```json
[
  {"entry_type": "artifact", "author": "executor-security", "tags": ["branch", "security"]},
  {"entry_type": "artifact", "author": "executor-performance", "tags": ["branch", "performance"]},
  {"entry_type": "artifact", "author": "executor-readability", "tags": ["branch", "readability"]},
  {"entry_type": "artifact", "author": "synthesizer-xxx", "tags": ["synthesis", "comprehensive"]},
  {"entry_type": "fact", "author": "validator-xxx", "content": {"valid": true}}
]
```

**В `logs/test_complex_pipelines.json` должен сохраниться пайплайн:**
```json
{
  "pipelines": [
    {
      "id": "pipe-xxx",
      "name": "multi_perspective_analysis",
      "task_pattern": "анализ.*перспектив",
      "steps": [
        {"primitive": "branch", "params": {"perspectives": ["security", "performance", "readability"]}},
        {"primitive": "synthesize"},
        {"primitive": "validate"}
      ],
      "success_score": 0.85,
      "usage_count": 1
    }
  ]
}
```

---

## Проверка логирования

### 1. Blackboard логи
Файлы: `logs/test_*_blackboard.json`

Что проверять:
- Каждое действие примитива оставляет запись
- Записи имеют правильный `entry_type` (artifact, critique, fact, etc.)
- Есть `author` (имя агента)
- Есть `tags` для фильтрации
- Timestamp в ISO формате

### 2. Pipeline Memory логи
Файлы: `logs/test_*_pipelines.json`

Что проверять:
- Успешные пайплайны сохраняются
- Есть `success_score`, `usage_count`
- Есть `task_pattern` для поиска похожих
- При повторной похожей задаче `usage_count` увеличивается

### 3. Agent логи
Файлы: `logs/agents_*.log`, `logs/verbose.log`

Система логирования уже существует в `utils/logger.py`:
- `JSONFormatter` - структурированные логи
- `LegacyFormatter` - старый формат
- `NonLockingFileHandler` - Windows-safe
- `TimestampedFileHandler` - файлы с timestamp

Все агенты автоматически логируют через эту систему.

---

## Ожидаемое поведение

### До исправлений (было):
```
Execute → пишет что сделал
Validate → пишет что проверил (но без инструментов)
❌ Агенты не видят blackboard
❌ Validation failure → только score *= 0.7
❌ Никаких итераций
```

### После исправлений (стало):
```
Execute → читает blackboard → выполняет с контекстом → пишет результат
Validate → читает blackboard → проверяет с инструментами → находит проблему
Revise → читает blackboard + feedback → исправляет → пишет улучшенную версию
Re-validate → проверяет исправление → valid: true
✅ Агенты видят работу друг друга
✅ Validation failure → автоматический revise loop
✅ До 3 итераций исправления
```

---

## Отладка

### Проблема: Агент не видит blackboard
**Проверить:**
1. `settings.social_intelligence.blackboard.enabled: true`
2. В `logs/xxx_blackboard.json` есть записи
3. В промпте агента должна быть секция `=== BLACKBOARD CONTEXT ===`

### Проблема: Validation не запускает revise
**Проверить:**
1. `settings.social_intelligence.refinement.enabled: true`
2. Validator вернул `valid: false`
3. В `_execute_pipeline()` есть блок `if not result.valid and max_refinement_iterations > 0`

### Проблема: Pipeline не сохраняется
**Проверить:**
1. `settings.social_intelligence.pipeline_memory.enabled: true`
2. `success_score >= 0.6` (min_success_score)
3. Файл `logs/xxx_pipelines.json` создаётся

---

## Результаты тестов

После запуска всех 4 тестов должно быть:

```
logs/
├── test_simple_blackboard.json          # Простые execute записи
├── test_critique_blackboard.json        # Execute + Critique + Revise
├── test_validation_blackboard.json      # Execute + Validate + Revise (iterations)
├── test_complex_blackboard.json         # Branch + Synthesize + Validate
├── test_simple_pipelines.json
├── test_critique_pipelines.json
├── test_validation_pipelines.json
├── test_complex_pipelines.json          # Сохранённый multi-perspective pipeline
└── agents_*.log                         # Детальные логи агентов
```

Все 5 проблем должны быть исправлены:
1. ✅ Blackboard read-only → теперь read-write
2. ✅ No iterative refinement → теперь validate → revise → re-validate
3. ✅ No tools for critics → теперь можно передать tools
4. ✅ Blind planner → теперь видит blackboard + pipeline memory
5. ✅ No cross-step context → теперь accumulated context
