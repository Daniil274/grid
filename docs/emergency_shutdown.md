# Emergency Shutdown для динамических агентов

## Обзор

Emergency Shutdown - механизм экстренной остановки всего pipeline при критической ошибке в одном из агентов. Позволяет агентам останавливать весь pipeline, очищать очередь задач, и предоставлять оркестратору информацию для перезапуска.

## Архитектура

### Компоненты

1. **PipelineRegistry** (`core/pipeline_registry.py`)
   - Singleton для отслеживания всех активных pipeline
   - Управление жизненным циклом задач
   - Emergency shutdown с graceful завершением (5 сек timeout)

2. **Emergency Tools** (`tools/emergency_tools.py`)
   - `emergency_shutdown(reason, severity)` - остановка pipeline
   - `get_pipeline_status()` - статус pipeline

3. **Orchestrator Integration** (`tools/orchestrator_tools.py`)
   - Автоматическая регистрация pipeline
   - Обработка CancelledError
   - Передача информации об остановке

4. **Agent Factory** (`core/agent_factory.py`)
   - Автоматическое добавление emergency_shutdown к агентам

## Использование

### Автоматическое использование

Emergency shutdown инструмент **автоматически добавляется** ко всем агентам внутри активного pipeline. Агентам не нужно явно запрашивать этот инструмент.

### Вызов из агента

```python
# Когда агент обнаруживает критическую проблему:
emergency_shutdown(
    reason="Database connection failed: Connection refused. All tasks require database access.",
    severity="critical"
)
```

### Параметры

- **reason** (required): Детальное описание причины остановки
  - Должно быть максимально конкретным
  - Помогает оркестратору принять решение о перезапуске

- **severity** (optional): Уровень критичности
  - `"critical"` (default) - критическая ошибка, система не может продолжить
  - `"error"` - серьезная ошибка, но система может продолжить
  - `"warning"` - проблема требует внимания

### Возвращаемое значение

JSON строка с информацией об остановке:

```json
{
  "success": true,
  "pipeline_id": "pipeline-a1b2c3d4",
  "reason": "Database unavailable",
  "severity": "critical",
  "cancelled_tasks": 3,
  "completed_tasks": 2,
  "failed_tasks": 1,
  "total_tasks": 6,
  "message": "Emergency shutdown executed..."
}
```

## Примеры использования

### Пример 1: Отсутствующая зависимость

```python
# Агент проверяет наличие критической зависимости
try:
    import required_library
except ImportError:
    emergency_shutdown(
        reason="Required library 'required_library' not found. Cannot proceed with task execution.",
        severity="critical"
    )
```

### Пример 2: Недоступный сервис

```python
# Агент пытается подключиться к базе данных
try:
    connection = connect_to_database()
except ConnectionError as e:
    emergency_shutdown(
        reason=f"Database connection failed after 3 retries: {e}. All subsequent tasks require database access.",
        severity="critical"
    )
```

### Пример 3: Обнаружение бесконечной рекурсии

```python
# Агент проверяет количество запущенных задач
status = get_pipeline_status()
status_data = json.loads(status)

if len(status_data["running_tasks"]) > 20:
    emergency_shutdown(
        reason="Pipeline spawned >20 concurrent tasks. Possible infinite recursion detected.",
        severity="error"
    )
```

## Обработка в оркестраторе

Когда происходит emergency shutdown, оркестратор получает результат с флагом `emergency_stopped=True`:

```python
result = orchestrate(task="Complex task")
result_data = json.loads(result)

if result_data.get("emergency_stopped"):
    print(f"Pipeline stopped: {result_data['emergency_reason']}")
    print(f"Severity: {result_data['emergency_severity']}")

    # Анализ ситуации и возможный перезапуск
    if result_data['emergency_severity'] == 'critical':
        # Критическая ошибка - требуется вмешательство
        notify_admin(result_data['emergency_reason'])
    else:
        # Некритическая ошибка - можно попробовать перезапустить
        retry_pipeline(result_data)
```

## Жизненный цикл Pipeline

### 1. Нормальное выполнение

```
orchestrate() вызван
    ↓
Pipeline зарегистрирован (pipeline-xxx)
    ↓
Агент создан с emergency_shutdown
    ↓
Task зарегистрирован и отслеживается
    ↓
Task выполняется успешно
    ↓
Task отмечен как completed
    ↓
Pipeline завершен успешно
```

### 2. Emergency Shutdown

```
orchestrate() вызван
    ↓
Pipeline зарегистрирован
    ↓
Несколько агентов запущены
    ↓
Агент #2 обнаруживает критическую проблему
    ↓
emergency_shutdown() вызван
    ↓
PipelineRegistry:
  - Находит все running tasks
  - Вызывает task.cancel() для каждой
  - Ждет graceful завершения (5 сек)
  - Статус → EMERGENCY_STOPPED
    ↓
Оркестратор ловит CancelledError
    ↓
Проверяет pipeline status
    ↓
Возвращает emergency_stopped=True
    ↓
Верхнеуровневый код анализирует и решает
```

## Технические детали

### Pipeline Statuses

- `RUNNING` - активно выполняется
- `COMPLETED` - успешно завершен
- `FAILED` - провален с ошибкой
- `EMERGENCY_STOPPED` - остановлен через emergency_shutdown
- `CANCELLING` - в процессе остановки

### Graceful Shutdown

Emergency shutdown использует graceful подход:

1. Все running tasks получают `task.cancel()`
2. Система ждет 5 секунд для завершения
3. После timeout процесс завершается принудительно

### Persistence

Критические события emergency shutdown сохраняются в MemoryStore для последующего анализа.

## Тестирование

Запустите тест для проверки функциональности:

```bash
python test_emergency_shutdown.py
```

Тест проверяет:
- Регистрацию pipeline
- Регистрацию tasks
- Emergency shutdown с несколькими tasks
- Graceful vs timeout shutdown
- Корректность статусов

## Best Practices

1. **Детальные причины**: Всегда указывайте максимально конкретную причину остановки
2. **Правильный severity**: Используйте `critical` только для действительно критических ошибок
3. **Проверка перед остановкой**: Убедитесь, что проблема действительно блокирует весь pipeline
4. **Логирование**: Логируйте контекст перед вызовом emergency_shutdown
5. **Альтернативы**: Рассмотрите возможность обработки ошибки без остановки pipeline

## Ограничения

1. Emergency shutdown останавливает ВЕСЬ pipeline - нет частичной остановки
2. Graceful shutdown ограничен 5 секундами
3. Механизм работает только внутри orchestrate() pipeline
4. Нет автоматического перезапуска - решение принимает оркестратор

## Troubleshooting

### Проблема: emergency_shutdown не доступен

**Решение**: Убедитесь, что агент создан внутри активного pipeline через orchestrate()

### Проблема: Tasks не останавливаются

**Решение**: Проверьте логи - возможно задачи не обрабатывают CancelledError

### Проблема: Pipeline status не обновляется

**Решение**: Проверьте, что PipelineRegistry правильно инициализирован в AgentFactory

## См. также

- [План реализации](../.claude/plans/concurrent-knitting-pumpkin.md)
- [PipelineRegistry API](../core/pipeline_registry.py)
- [Emergency Tools API](../tools/emergency_tools.py)
