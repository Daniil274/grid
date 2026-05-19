# Coordinator: паттерны конвейеров

## Паттерн A — простая цепочка (2 агента)

Сбор информации → исполнитель.

```
1. beads_create(...) → bd-XXXX
2. call_web_agent("Найди X. Запиши в bd-XXXX")
3. call_code_agent("Используй bd-XXXX, напиши код")
4. beads_close("bd-XXXX")
```

## Паттерн B — контекст → исполнение → верификация (3 агента)

```
1. beads_create("Сбор контекста") → bd-CTX
2. beads_create("Реализация") → bd-IMPL
3. beads_dep(add, child=bd-IMPL, parent=bd-CTX)
4. orchestrate(
     task="Собери контекст, bead_id=bd-CTX, ...",
     system_skills=["beads"],
     executor_tools=["beads_show","beads_update","file_read","glob_tool","grep_tool"],
   )
5. call_code_agent("bead_id=bd-IMPL, контекст в bd-CTX, beads_show → работа → beads_update...")
6. orchestrate(
     task="Верифицируй bd-IMPL, bead_id=bd-IMPL, ...",
     system_skills=["beads"],
     executor_tools=["beads_show","beads_update","file_read","grep_tool"],
   )
7. beads_show("bd-IMPL") → итог
```

Beads создавай **до** запуска агентов; зависимости — после получения всех id.
