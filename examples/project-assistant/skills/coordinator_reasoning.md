# Coordinator: фреймворк рассуждения

При новой задаче **сначала рассуждай**, не вызывай агентов сразу.

## Шаг 0 — тривиальность

Тривиальная → ответ сам (skill `coordinator_core`). Иначе → конвейер через beads обязателен.

## Шаг A — классификация

- **Тип:** исследование | создание | рефакторинг | исправление | анализ | смешанный
- **Масштаб:** атомарная (1 агент) | составная (2–3) | комплексная (4+)

## Шаг B — паттерн

- Атомарная → паттерн A (+ верификатор при изменениях)
- Составная / комплексная → паттерн B (skill `coordinator_patterns`)

## Шаг C — beads до агентов

1. `beads_create` для всех задач pipeline (одним ответом)
2. `beads_dep` при необходимости
3. Затем `orchestrate` / `call_code_agent` / `call_web_agent`
4. На каждый `orchestrate`: `system_skills=["beads"]`, в `executor_tools` — `beads_show`, `beads_update`, в `task` — `bead_id` (skill `coordinator_delegation`)
