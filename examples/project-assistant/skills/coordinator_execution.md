# Coordinator: serial runtime и пакетирование

## Последовательное исполнение (depth-first)

Runtime — serial subtree:
- sibling-вызовы последовательно в порядке вызова;
- поддерево A завершается полностью, затем sibling B;
- для `orchestrate`, agent-tools и function-tools.

Между уже запланированными шагами **нет** нового reasoning-loop. Шаг 2 зависит от шага 1 только через beads, `context_id`, файлы, память — не через промежуточные размышления координатора.

Несколько `orchestrate` / `call_*` в одном ходе допустимо, если это заранее спланированный compiled pipeline.

## Пакетирование (обязательно)

Независимые инструменты — **в одном ответе**. Один tool call на ход — антипаттерн.

- `beads_create` — все beads pipeline в одном ответе
- `beads_dep` — все зависимости в одном ответе после получения id
- `file_read`, `glob_tool`, `grep_tool` — параллельно, если независимы
- `beads_close`, `beads_update` для разных id — в одном ответе
