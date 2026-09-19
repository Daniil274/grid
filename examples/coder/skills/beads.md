# Beads Issue Tracking

Используй beads как персистентную шину данных между агентами. Агенты не видят друг друга напрямую — передавай `bead_id` и записывай результаты в bead.

## Activation

Применяй эти инструкции, когда в проекте есть `.beads` или координатор передал `bead_id`. Если `.beads` нет — сначала `beads_init(directory=...)`.

## Session Start

В начале сессии: `beads_init` (идемпотентно), затем `beads_ready` для контекста «что готово к работе».

## Checking What's Next

На «What's next?» / «что дальше?» — `beads_ready`, затем краткий список:

```
In-progress:
- <id>: <title>

Ready:
- <id>: <title>
```

## Creating Issues

«Add task» / «Add epic» → `beads_create` с подходящим `type` (`task`, `epic`, `message`).

Осмысленные заголовки и описания; для эпиков — `type=epic`, для подзадач — зависимости через `beads_dep`.

## Task State Management

Перед работой — `beads_update` (in progress). По завершении — результат в `notes`/`description`, затем `beads_close`.

PR/MR URL — в метаданных bead, если релевантно.

## Протокол handoff между агентами

**Координатор (до вызова агента):**
```
beads_create(title="...", type="task", description="GOAL, SCOPE, DONE CRITERIA...")
→ bead_id, например "bd-a1b2"
```

**Исполнитель:**
```
beads_show("bd-a1b2")  → вход
... работа ...
beads_update("bd-a1b2", notes="результат, файлы, верификация")
```

**Координатор (после):**
```
beads_show("bd-a1b2") → beads_close(..., reason="...")
```

Минимальный handoff packet в `description`/`notes`: GOAL, SCOPE, INPUTS (bead_id, файлы), OUTPUTS, DONE CRITERIA, LIMITS.

## Инструменты (GRID)

| Действие | Инструмент |
|----------|------------|
| Инициализация | `beads_init` |
| Готово к работе | `beads_ready` |
| Список | `beads_list` |
| Создать | `beads_create` |
| Детали | `beads_show` |
| Обновить | `beads_update` |
| Закрыть | `beads_close` |
| Зависимости | `beads_dep` |
| Синхронизация | `beads_sync` |
| Общий лог | `beads_log_append`, `beads_log_read` |

Независимые `beads_create` / `beads_dep` / `beads_close` для разных id — пакетируй в одном ответе.

## Session Completion (код в репозитории)

Перед завершением сессии с изменениями кода:
1. Зафиксируй оставшуюся работу в beads
2. Quality gates (тесты, линтер)
3. `beads_close` / обнови статусы
4. `beads_sync`, git push
5. Handoff в notes: что делать следующему агенту

Критично: не оставляй незакоммиченную работу без bead; не закрывай задачу без верификации на сложных изменениях.
