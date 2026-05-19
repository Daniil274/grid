# Coordinator: делегирование и handoff

## Инструменты делегирования

- `orchestrate(task, agent_system_prompt, model_key, executor_tools, system_skills, ...)` — динамический агент; каждый `task` самодостаточен.
- `call_code_agent` — программирование; передай `bead_id` и чёткие инструкции.
- `call_web_agent` — поиск в интернете; задача и ожидаемый формат результата.

Default model для `orchestrate`: `deepseek-v4-flash-opencode` (см. конфиг tool).

## Skill `beads` — обязателен для каждого исполнителя

Любой вызов `orchestrate` и любая подзадача через `code_agent` / `web_agent` работает через beads. Координатор **всегда** подключает skill `beads` исполнителю:

| Способ делегирования | Как передать skill `beads` |
|----------------------|----------------------------|
| `orchestrate` | `system_skills: ["beads"]` |
| `code_agent`, `web_agent` | Уже в `system_skills` агента в конфиге; в `task` явно укажи `bead_id` и протокол из skill `beads` |

**`orchestrate` — минимальный набор параметров на шаг pipeline:**

```
orchestrate(
  task="... GOAL, bead_id=bd-XXXX, DONE CRITERIA ...",
  agent_system_prompt="Роль: ... Сначала beads_show(bd-XXXX). Результат — beads_update(...).",
  system_skills=["beads"],
  executor_tools=["beads_show", "beads_update", ...остальные по роли...],
)
```

В `executor_tools` для исполнителя **всегда** включай как минимум `beads_show` и `beads_update`. Для сбора контекста добавь `file_read`, `glob_tool`, `grep_tool`; для кода — `bash_tool`, `file_read`, `file_write`, `file_edit`; для веба — `web_search`, `web_fetch`.

В `agent_system_prompt` одной строкой: «Следуй skill beads: вход через `beads_show(bead_id)`, выход через `beads_update` с notes».

Не запускай исполнителя без `bead_id` в `task` и без skill `beads` — иначе handoff между шагами pipeline теряется.

## Handoff packet

Любой `orchestrate` / agent-tool передаёт сжатый пакет:

- **GOAL** — что получить
- **SCOPE** — границы подзадачи
- **INPUTS** — bead_id, файлы, артефакты
- **OUTPUTS** — что должно появиться
- **DONE CRITERIA** — критерий готовности
- **LIMITS** — инструменты, время, область изменений

Не передавай длинную историю и сырые рассуждения — только `bead_id` и краткое резюме. Детали протокола beads — skill `beads`.
