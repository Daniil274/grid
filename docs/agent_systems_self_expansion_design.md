# Дизайн самоулучшения и саморасширения через системы-нодЫ

## Цель

Нужен такой дизайн, при котором агент может:

- улучшать существующие системы;
- создавать новые системы;
- подключать их в общий реестр;
- вызывать их как обычные исполняемые объекты;
- безопасно тестировать версии до публикации;
- не ломать runtime и не плодить special-case логику.

Ключевое требование: всё должно быть логично, просто и однообразно.

Поэтому основная идея такая:

- всё исполняемое в платформе представляется как `node`;
- всё композиционное представляется как `system`;
- все изменения происходят через создание новой `version`;
- всё подключение идёт через `registry` и `release channels`;
- самоулучшение и саморасширение используют один и тот же lifecycle.

---

## 1. Единая ментальная модель

### Главные сущности

#### `Node`

Минимальная исполняемая единица.

Типы:

- `agent`
- `tool`
- `router`
- `workflow`
- `system`
- `evaluator`

#### `System`

Композиция из нод с:

- входной точкой;
- интерфейсом;
- графом исполнения;
- зависимостями;
- policy;
- версиями.

#### `Version`

Неизменяемый снимок определения системы.

#### `Release Channel`

Указатель на активную версию:

- `stable`
- `candidate`
- `canary`
- `dev`
- `archived`

#### `Registry`

Реестр, который отвечает на вопросы:

- какие системы существуют;
- какие версии у них есть;
- какая версия активна в каком канале;
- кто от кого зависит;
- какие интерфейсы экспортируются.

---

## 2. Один lifecycle для улучшения и расширения

Самоулучшение и саморасширение не должны быть двумя разными подсистемами.

Они отличаются только базовой операцией:

- self-improvement: создать новую версию существующей системы;
- self-expansion: создать новую систему и зарегистрировать её.

Дальше lifecycle одинаковый:

1. Инициатива
2. Проектирование
3. Сборка definition
4. Регистрация candidate version
5. Проверка интерфейса
6. Benchmark / simulation
7. Canary
8. Promotion
9. Наблюдение

Это важно: если self-expansion не проходит тот же pipeline, он очень быстро превратится в хаотическую генерацию новых YAML-файлов.

---

## 3. Три уровня изменений

Чтобы система была логичной, надо явно разделить виды изменений.

### Уровень A. `Tune`

Малые изменения внутри существующей системы:

- поменять модель ноды;
- поменять prompt;
- поменять набор tools;
- поменять routing rule;
- поменять policy;
- поменять default agent.

Это self-improvement.

### Уровень B. `Extend`

Добавление новых внутренних возможностей:

- новая нода в существующую систему;
- новый subworkflow;
- новый evaluator;
- новая ветка маршрутизации.

Это уже частично self-expansion, но внутри существующей системы.

### Уровень C. `Create`

Создание новой системы:

- новый `system_id`;
- новый интерфейс;
- новый граф;
- новый lifecycle;
- публикация в registry.

Это полноценное self-expansion.

Все три уровня должны использовать одинаковые доменные операции.

---

## 4. Архитектурный принцип простоты

Самая опасная ошибка здесь: дать агенту доступ к редактированию произвольного конфига.

Правильный дизайн:

- агент не редактирует структуру платформы напрямую;
- агент вызывает доменные операции;
- доменные операции строят валидный definition;
- runtime исполняет только зарегистрированные версии.

То есть вместо:

- "открой YAML и впиши что-то"

должно быть:

- `create_system(...)`
- `fork_system_version(...)`
- `add_node(...)`
- `connect_nodes(...)`
- `set_entrypoint(...)`
- `publish_candidate(...)`

Это и есть главный признак некостыльной архитектуры.

---

## 5. Целевая структура данных

## 5.1 `SystemDefinition`

```yaml
id: coding_assistant
title: Coding Assistant
description: Система для инженерных задач
kind: system
entrypoint: main
default_agent: chat_agent
interfaces:
  invoke:
    input_schema: coding_task_v1
    output_schema: coding_result_v1
exports:
  capabilities: [code, planning, refactor]
  tags: [engineering]
nodes:
  main:
    type: agent
    ref: agents.chat_agent
  planner:
    type: agent
    ref: agents.coordinator
  coder:
    type: agent
    ref: agents.code_agent
edges:
  - from: main
    to: planner
    when: input.task_complexity == "high"
  - from: planner
    to: coder
    when: state.next_step == "implementation"
policies:
  execution_policy: default
  safety_policy: safe_evolution
dependencies:
  systems: []
metadata:
  owner: system
```

### Что важно

- `ref` указывает на уже существующие сущности;
- `nodes` и `edges` задают graph;
- `interfaces` делают систему вызываемой извне;
- `exports.capabilities` дают механизмы discovery;
- `dependencies` позволяют строить сеть систем.

## 5.2 `SystemVersion`

```yaml
system_id: coding_assistant
version: 1.3.0
status: candidate
parent_version: 1.2.0
definition_ref: systems/coding_assistant/versions/1.3.0.yaml
definition_hash: sha256:...
created_by: agent
created_at: ...
source:
  kind: experiment
  id: exp-123
compatibility:
  input_schema: coding_task_v1
  output_schema: coding_result_v1
evaluation:
  benchmark_suite: coding_regression
  last_score: 0.84
```

## 5.3 `SystemRelease`

```yaml
system_id: coding_assistant
channels:
  stable: 1.2.0
  candidate: 1.3.0
  canary: 1.3.0
```

## 5.4 `SystemManifest`

Нужен короткий индекс для интроспекции и discovery.

```yaml
system_id: coding_assistant
title: Coding Assistant
description: Система для инженерных задач
latest_stable: 1.2.0
capabilities: [code, planning, refactor]
entrypoint: main
input_schema: coding_task_v1
output_schema: coding_result_v1
discoverable: true
invokable: true
```

---

## 6. Как агент должен создавать новую систему

Self-expansion должно быть операцией первого класса.

### Pipeline создания новой системы

1. Агент формулирует потребность
2. Агент проверяет, нет ли уже похожей системы
3. Агент проектирует интерфейс
4. Агент проектирует граф
5. Агент собирает definition
6. Система валидируется
7. Создаётся версия `0.1.0-candidate`
8. Прогоняются smoke tests
9. Система регистрируется как discoverable
10. После оценки публикуется в `stable`

### Что именно должен решить агент до создания

- какова цель новой системы;
- какой у неё входной контракт;
- какой ожидается выход;
- какие capabilities она экспортирует;
- является ли она leaf-системой или orchestration-системой;
- может ли она вызывать другие системы;
- какие policies ей нужны.

### Что нельзя разрешать

Агент не должен:

- сразу писать в `stable`;
- переписывать чужие stable-версии;
- подменять channel без evaluation;
- создавать систему без интерфейса и capabilities;
- создавать циклические зависимости без явного разрешения.

---

## 7. Self-expansion как сетевое расширение платформы

Новая система полезна только если её можно обнаружить и вызвать.

Поэтому после создания должны происходить две вещи:

### 1. `Registration`

Система попадает в `SystemRegistry` и становится видимой через discovery tools.

### 2. `Capability Export`

Система объявляет:

- что она умеет;
- какой контракт у вызова;
- какие ограничения у исполнения.

Тогда другие агенты и системы могут находить её по capability, а не только по имени.

Пример:

- агент ищет `capability=browser_automation`;
- registry возвращает `web_operator_system`;
- агент вызывает её по контракту.

Это сильно лучше, чем жёстко прошивать знание о новых системах в prompts.

---

## 8. Discovery и вызов

Чтобы всё оставалось простым, нужен стандартный внешний API.

## 8.1 Discovery API

Инструменты:

- `system_list_systems`
- `system_search_systems`
- `system_get_system_info`
- `system_get_system_versions`
- `system_get_system_dependencies`

## 8.2 Invocation API

Инструменты:

- `system_invoke`
- `system_invoke_version`
- `system_invoke_capability`

### Пример вызова по ID

```json
{
  "system_id": "coding_assistant",
  "channel": "stable",
  "input": {
    "task": "Найди причину flaky test"
  }
}
```

### Пример вызова по capability

```json
{
  "capability": "document_analysis",
  "channel": "stable",
  "input": {
    "file_path": "docs/spec.pdf",
    "goal": "извлечь требования"
  }
}
```

### Что делает runtime

1. резолвит систему;
2. резолвит канал в версию;
3. загружает definition;
4. компилирует в graph IR;
5. исполняет entrypoint;
6. возвращает структурированный result.

---

## 9. Два режима исполнения системы

Чтобы не усложнять внедрение, система должна поддерживать два режима.

### Режим 1. `Proxy Mode`

Для раннего этапа.

Система просто проксируется на `default_agent`.

Плюсы:

- легко внедрить;
- обратная совместимость;
- можно быстро подключить `systems:` без переписывания runtime.

### Режим 2. `Graph Mode`

Полноценный runtime.

Система исполняется как граф нод.

Плюсы:

- настоящая композиция;
- self-expansion может строить внутренние структуры;
- evaluator видит реальное поведение системы;
- dependency management становится чистым.

Правильный путь: сначала Proxy Mode, потом Graph Mode.

---

## 10. Дизайн self-improvement

Self-improvement должен опираться не на "diff файла", а на "diff definition".

### Улучшение системы выглядит так

1. выбрать `target_system`;
2. выбрать `base_version`;
3. создать fork candidate version;
4. применить change-set к definition;
5. прогнать validation;
6. прогнать benchmark;
7. записать результаты;
8. переключить candidate/stable channel при успехе.

### Типы улучшений

- заменить модель ноды;
- переписать prompt ноды;
- сменить edge conditions;
- добавить evaluator;
- сузить или расширить tools;
- изменить default entry behavior.

### Change-set лучше хранить как доменные мутации

Не так:

```json
{"path":"agents.chat_agent.model","new":"x"}
```

А так:

```json
{
  "op": "replace_node_agent_model",
  "node_id": "main",
  "new_model": "gpt-5.4-mini"
}
```

Или:

```json
{
  "op": "add_node",
  "node": {
    "id": "validator",
    "type": "agent",
    "ref": "agents.reviewer"
  }
}
```

Это намного устойчивее к будущим изменениям структуры.

---

## 11. Дизайн self-expansion

Self-expansion должен быть ограниченным конструктором новых систем.

### Базовый workflow

1. Агент обнаруживает unmet capability
2. Проверяет существующие системы
3. Формирует proposal новой системы
4. Создаёт draft definition
5. Запускает sandbox validation
6. Регистрирует candidate system
7. Запускает smoke/evaluation
8. Делает систему discoverable
9. Подключает её как dependency в нужные системы

### Self-expansion может происходить в двух формах

#### `Standalone creation`

Новая независимая система.

Пример:

- `research_system`
- `browser_operator_system`
- `test_repair_system`

#### `Inline extraction`

Агент выделяет повторяющийся кусок логики из большой системы в новую специализированную систему.

Пример:

- из большого `coding_assistant` выделяется `test_debugger_system`;
- потом `coding_assistant` начинает вызывать его как dependency.

Это очень важный механизм нормального роста архитектуры.

---

## 12. Саморасширение без бардака: правила

Чтобы платформа не утонула в системах-клонах, нужны жёсткие правила.

### Правило 1. Capability-first

Новая система должна иметь новую или существенно улучшенную capability.

### Правило 2. Interface-first

Нельзя регистрировать систему без формального входного и выходного контракта.

### Правило 3. Discoverability

Новая система обязана публиковать `title`, `description`, `capabilities`, `constraints`.

### Правило 4. Evaluation-first

Нельзя публиковать в `stable` без smoke tests и минимального benchmark.

### Правило 5. Ownership

У каждой системы есть owner:

- `human`
- `system`
- `shared`

### Правило 6. Dependency hygiene

Нельзя создавать циклические зависимости без специального флага policy.

### Правило 7. Budget

У self-expansion должен быть бюджет:

- максимум новых систем за период;
- максимум candidate-версий;
- максимум одновременно активных canary.

---

## 13. Какие новые инструменты нужны агенту

## 13.1 Registry tools

- `system_list_systems`
- `system_search_systems`
- `system_get_system_info`
- `system_get_system_versions`
- `system_get_release_channels`

## 13.2 Design tools

- `system_create_draft`
- `system_clone_version`
- `system_validate_definition`
- `system_diff_versions`
- `system_estimate_dependencies`

## 13.3 Mutation tools

- `system_add_node`
- `system_remove_node`
- `system_update_node`
- `system_connect_nodes`
- `system_disconnect_nodes`
- `system_set_entrypoint`
- `system_set_interface`
- `system_set_policy`
- `system_add_dependency`

## 13.4 Publication tools

- `system_register_candidate`
- `system_run_smoke_test`
- `system_run_benchmark`
- `system_promote_channel`
- `system_archive_version`

## 13.5 Invocation tools

- `system_invoke`
- `system_invoke_version`
- `system_invoke_capability`

Это и есть тот bounded API, через который агент сможет строить и использовать новые системы.

---

## 14. Что должен делать компилятор

Между definition и runtime нужен `SystemCompiler`.

### Его задачи

1. нормализовать refs;
2. проверить, что все ноды существуют;
3. проверить, что entrypoint валиден;
4. проверить интерфейсы;
5. проверить policies;
6. обнаружить циклы и некорректные зависимости;
7. построить graph IR.

### Результат компиляции

Примерный IR:

```json
{
  "system_id": "coding_assistant",
  "version": "1.3.0",
  "entrypoint": "main",
  "nodes": {
    "main": {"runtime_type": "agent", "target": "chat_agent"},
    "planner": {"runtime_type": "agent", "target": "coordinator"}
  },
  "edges": [...],
  "policies": {...}
}
```

Runtime потом работает только с IR.

Это позволяет:

- менять формат хранения definitions;
- не усложнять runtime YAML-логикой;
- кэшировать compiled systems.

---

## 15. Что должен делать runtime

`SystemRuntime` должен иметь один главный метод:

```python
invoke(system_ref, input_payload, context, execution_policy) -> SystemRunResult
```

### Он обязан уметь

- запускать систему по `system_id + channel`;
- запускать по `system_id + version`;
- запускать dependency systems;
- наследовать execution context;
- ограничивать глубину вложенности;
- логировать run на уровне system/version/node.

### Что важно логировать

- `system_id`
- `version`
- `channel`
- `run_id`
- `parent_run_id`
- `node_path`
- `status`
- `latency`
- `tool_cost`
- `errors`

Без version-aware наблюдаемости self-improvement будет слепым.

---

## 16. Как подключать новую систему в другие системы

Новая система должна быть доступна как dependency node.

Пример:

```yaml
nodes:
  test_repair:
    type: system
    ref: systems.test_repair_system
```

или version-pinned:

```yaml
nodes:
  test_repair:
    type: system
    ref: systems.test_repair_system@1.1.0
```

### Политика использования

- по умолчанию dependency идёт на `stable`;
- в candidate-версиях можно пинить на candidate dependency;
- stable не должна зависеть от candidate без специального policy.

Это защищает от лавинообразной нестабильности.

---

## 17. Версионирование и совместимость

Чтобы всё было просто, правила версионирования должны быть короткими.

### Версии систем

- `MAJOR`: ломается интерфейс
- `MINOR`: новые возможности без ломки интерфейса
- `PATCH`: внутренняя настройка/фиксы

### Совместимость

Каждая система должна объявлять:

- `input_schema_version`
- `output_schema_version`

### Правило

Если входной или выходной контракт несовместим, нельзя автоматически двигать `stable`.

---

## 18. Что делать с текущим improvement loop

Его не надо выкидывать. Его надо переосмыслить.

Сейчас он работает вокруг `problem -> experiment -> config_diff -> evaluation`.

Новая форма:

- `problem -> target_system -> candidate_version -> evaluation -> promotion`

### Что можно сохранить

- backlog проблем;
- reviews;
- benchmark evaluation;
- canary;
- rollback.

### Что надо заменить

- вместо `config_diff` использовать `system_mutation_set`;
- вместо применения diff к live config делать `candidate version build`;
- вместо promotion диффа двигать release channel.

---

## 19. Минимальная простая структура файлов

Чтобы агент мог сам создавать системы, хранение должно быть прозрачным.

### Рекомендуемая структура

```text
config/
  agents/
    chat_agent.yaml
    code_agent.yaml
    coordinator.yaml
  systems/
    coding_assistant/
      manifest.yaml
      releases.yaml
      versions/
        1.0.0.yaml
        1.1.0.yaml
    document_analysis/
      manifest.yaml
      releases.yaml
      versions/
        0.1.0.yaml
registry/
  systems_index.json
  dependencies.json
data/
  improvement_registry.json
```

### Почему это удобно

- агенту легко создавать новую систему как новую директорию;
- версия — это отдельный файл;
- release channels не смешаны с definition;
- сравнение и аудит просты;
- rollback тривиален.

---

## 20. Минимальные классы для реализации

### В `schemas/`

- `SystemDefinition`
- `SystemNode`
- `SystemEdge`
- `SystemInterface`
- `SystemVersion`
- `SystemReleaseState`
- `SystemMutation`
- `SystemRunResult`

### В `core/`

- `system_registry.py`
- `system_compiler.py`
- `system_runtime.py`
- `system_mutator.py`
- `system_release_manager.py`
- `system_discovery.py`

### В `tools/`

- `system_registry_tools.py`
- `system_design_tools.py`
- `system_runtime_tools.py`
- `system_release_tools.py`

---

## 21. Поэтапное внедрение без перегруза

## Этап 1. Registry-first

Сделать:

- `SystemDefinition`
- `SystemVersion`
- `SystemRegistry`
- `system_list_systems`
- `system_get_system_info`

Пока без сложного runtime.

## Этап 2. Proxy invocation

Сделать:

- `system_invoke`
- `system_create_draft`
- `system_register_candidate`

Система вызывается через `default_agent`.

## Этап 3. Mutation API

Сделать:

- операции изменения system definition;
- candidate version lifecycle;
- version-aware validation.

## Этап 4. Graph runtime

Сделать:

- compiler;
- graph IR;
- execution engine для system nodes.

## Этап 5. Improvement migration

Перевести current improvement loop на system versions.

## Этап 6. Self-expansion

Разрешить агентам:

- создавать новые системы;
- публиковать candidate;
- подключать dependency;
- использовать capability discovery.

---

## 22. Самое важное упрощение

Если нужно удержать всё простым, надо помнить одно правило:

### Агент не управляет конфигом.

### Агент управляет реестром систем и версиями систем через доменные операции.

Это центральный принцип.

Пока он соблюдается:

- self-improvement остаётся контролируемым;
- self-expansion остаётся осмысленным;
- архитектура не превращается в набор хакающих YAML агентов;
- новые системы становятся частью платформы, а не мусором в проекте.

---

## 23. Итоговая формула

Правильная платформа должна мыслиться так:

- `agent` решает задачи;
- `system` композиционирует capability;
- `registry` делает системы обнаружимыми;
- `version` делает изменения безопасными;
- `channels` делают релиз управляемым;
- `evaluation` делает рост проверяемым;
- `mutation API` делает самоизменение структурным;
- `self-expansion` — это создание новых систем через тот же lifecycle.

Тогда агент действительно сможет:

- придумать новую систему;
- спроектировать интерфейс;
- собрать graph;
- зарегистрировать candidate;
- протестировать;
- подключить её как dependency;
- начать использовать;

и всё это будет не костылём, а естественным поведением платформы.

---

## 24. Архитектурные коррекции

Этот раздел уточняет и ужесточает дизайн там, где в базовой версии документа были допущены неоднозначности.

## 24.1 Терминология и рекурсия

Чтобы убрать петлю между `system` как типом ноды и `System` как верхнеуровневой сущностью, вводятся три разных термина.

### `ExecutableNode`

Типы исполняемых нод runtime:

- `agent_node`
- `tool_node`
- `router_node`
- `workflow_node`
- `system_ref_node`
- `evaluator_node`

### `SystemDefinition`

Это не node type, а versioned graph artifact.

У `SystemDefinition` есть:

- `entrypoint`
- `interface`
- `graph`
- `policy`
- `version`

### `SystemRefNode`

Единственный допустимый способ вложить систему в систему.

Пример:

```yaml
nodes:
  docs_worker:
    type: system_ref_node
    target_system: document_analysis
    target_channel: stable
```

### Ограничение глубины

Рекурсия допускается только как nested invocation через `system_ref_node`.

Должен существовать явный лимит:

```yaml
runtime_limits:
  max_system_call_depth: 3
```

Проверки:

- compiler отклоняет статически обнаруживаемые циклы;
- runtime отклоняет превышение `max_system_call_depth`.

## 24.2 Edge conditions без eval

Свободные строковые выражения для `when:` запрещены.

Плохо:

```yaml
when: input.task_complexity == "high"
```

Правильно:

```yaml
when:
  op: eq
  left:
    var: input.task_complexity
  right:
    value: high
```

Или:

```yaml
when:
  op: and
  args:
    - op: eq
      left: { var: input.kind }
      right: { value: coding }
    - op: in
      left: { var: input.priority }
      right: { value: [high, urgent] }
```

### Кто вычисляет

`ConditionEvaluator`, а не `eval`.

`ConditionEvaluator`:

- понимает только allowlisted операции;
- работает только с типизированным runtime context;
- не умеет выполнять код;
- не имеет доступа к imports, FS, network и Python objects.

Допустимые операции:

- `eq`
- `neq`
- `in`
- `not_in`
- `gt`
- `gte`
- `lt`
- `lte`
- `exists`
- `and`
- `or`
- `not`

Допустимые источники значений:

- `input.*`
- `state.*`
- `node_output.<node_id>.*`
- `context.flags.*`

Enforcement:

- compiler валидирует shape predicate и допустимость операторов;
- runtime валидирует типы и наличие данных.

## 24.3 Правила должны быть enforceable

Правила из раздела 12 не являются пожеланиями. Они должны принуждаться тремя слоями.

### `Compiler Enforcement`

Проверяет:

- interface-first;
- dependency hygiene;
- schema compatibility;
- корректность graph;
- корректность `system_ref_node`;
- допустимость predicates.

### `Registry Enforcement`

Проверяет:

- lifecycle transitions;
- uniqueness versions;
- channel move preconditions;
- budget limits;
- ownership and permissions;
- required evaluation artifacts.

### `Runtime Enforcement`

Проверяет:

- max depth;
- invocation permissions;
- sandbox profile;
- resource ceilings;
- failure policy;
- side-effect policy.

Итог:

- каждое архитектурное правило должно быть представлено как compile-time, registry-time или runtime invariant.

## 24.4 Budget как подсистема

Budget должен быть формализован как policy и храниться в control state store.

Пример:

```yaml
budgets:
  self_expansion:
    max_new_systems_per_day: 2
    max_candidate_versions_per_system: 3
    max_active_canaries: 2
    max_promotions_per_day: 5
    reset_window: daily
    hard_fail_on_exceed: true
```

### Счётчики

Минимально нужны:

- `new_systems_created_today`
- `candidate_versions_open[system_id]`
- `active_canaries`
- `promotions_today`

### Где хранятся

Не в памяти runtime, а в registry backend или связанном control store.

### Кто обновляет

- `create_system`
- `register_candidate`
- `start_canary`
- `promote_channel`
- `archive_candidate`
- `reject_candidate`

### Кто сбрасывает

- windowed scheduler;
- или rolling-window evaluator.

### Поведение при превышении

- `hard_fail` в production;
- `soft_warning` допустим только в dev/test.

Все budget checks должны выполняться атомарно внутри registry transaction.

## 24.5 Lifecycle ранних стадий внедрения

На Этапах 1-2 итоговый production lifecycle ещё недоступен.

Поэтому lifecycle нужно описывать как staged.

### Stage A. Registry-first

Есть:

- draft;
- schema validation;
- registry entry;
- manual review.

Нет:

- canary;
- graph runtime;
- полноценного benchmark orchestration.

### Stage B. Proxy invocation

Есть:

- invocation через `default_agent`;
- smoke tests;
- candidate channel;
- manual promotion gate.

Нет:

- node-level failure semantics;
- nested system runtime;
- full release automation.

### Stage C. Full graph runtime

Есть:

- graph execution;
- canary;
- failure policies;
- budget enforcement;
- release channel controls.

Следовательно, lifecycle из раздела 2 является целевой production-моделью, а не literal-моделью для Stage A-B.

## 24.6 Benchmark governance и ground truth

Если агент сам строит систему и сам пишет benchmark, это недостаточно для stable promotion.

Нужно явно различать provenance benchmark suite.

### Источники suites

- `human_curated`
- `production_trace_curated`
- `agent_proposed_pending_review`
- `synthetic_low_confidence`

### Уровни доверия

- `gold`
- `silver`
- `bronze`

### Правило promotion

- `stable` promotion опирается только на `gold` и `silver`;
- `bronze` допустим только для раннего screening candidate;
- cold-start systems не могут auto-promote в `stable` только по synthetic benchmarks.

### Кто курирует benchmark

- человек;
- или trusted governance process, отделённый от builder-agent.

## 24.7 Concurrency и write contention

Многоагентная среда требует явной модели конкурентных мутаций.

### Конфликты

- два candidate для одной системы;
- одновременная запись в release channels;
- параллельные promotions;
- запись в системный индекс;
- обновление budget counters.

### Минимальные требования

- transactional registry backend;
- revision number у system state;
- per-system lock;
- compare-and-swap для channel promotion.

### Promotion

Promotion должен работать так:

1. прочитать current channel version;
2. попытаться обновить только если version всё ещё ожидаемая;
3. при расхождении вернуть `channel_conflict`.

### Замечание по backend

Для write-heavy сценариев JSON-файлы плохая основа.

Если на старте остаётся JSON:

- нужен lock manager или single writer process.

Целевой вариант:

- SQLite или другой transactional store.

## 24.8 Security model

Self-expansion без security model недопустим.

### Субъекты

- `human_admin`
- `human_reviewer`
- `system_agent`
- `builder_agent`
- `runtime_agent`
- `observer_agent`

### Действия

- `create_system`
- `create_candidate`
- `modify_definition`
- `run_candidate`
- `promote_candidate`
- `move_stable_channel`
- `invoke_system`
- `add_dependency`

### Модель прав

Capability-based access control.

Пример:

```yaml
permissions:
  roles:
    builder_agent:
      allow:
        - create_system
        - create_candidate
        - modify_definition
        - run_candidate
      deny:
        - move_stable_channel
    system_agent:
      allow:
        - invoke_system
        - search_systems
    human_admin:
      allow:
        - "*"
```

### Базовые ограничения

- не любой агент может создавать системы;
- не любой агент может двигать `stable`;
- candidate systems исполняются только в sandbox profile;
- stable system не зависит от untrusted candidate без explicit policy exception;
- cross-system invocation проходит permission check.

## 24.9 Failure propagation и rollback

В Graph Mode failure semantics должны быть частью модели.

У каждой ноды должен быть `failure_policy`.

Пример:

```yaml
nodes:
  planner:
    type: agent_node
    ref: agents.coordinator
    failure_policy:
      on_error: fail_run
      retry:
        max_attempts: 1

  optional_docs:
    type: system_ref_node
    target_system: document_analysis
    failure_policy:
      on_error: continue_with_warning
      retry:
        max_attempts: 2
        backoff_ms: 500
```

Допустимые режимы:

- `fail_run`
- `continue_with_warning`
- `skip_node`
- `fallback_to_node`

`SystemRunResult` должен содержать:

- `status`
- `final_output`
- `node_results[]`
- `failed_nodes[]`
- `warnings[]`
- `retry_trace[]`
- `side_effects[]`

### Что такое rollback

Нужно разделять:

- `release rollback`
  Сдвиг канала на предыдущую версию.
- `state rollback`
  Откат только reversible side effects.
- `no rollback possible`
  Для необратимых внешних эффектов.

Поэтому каждый side effect должен быть размечен:

- `reversible`
- `irreversible`
- `compensation_handler`

Без этого rollback нельзя называть тривиальным.

## 24.10 Cold-start режим для self-expansion

Структурно self-improvement и self-expansion используют похожий lifecycle, но epistemic situation у них разная.

Для новых систем нужен отдельный режим:

### `cold_start_expansion`

Шаги:

1. `proposal`
2. `interface review`
3. `definition validation`
4. `smoke benchmark`
5. `sandbox trial`
6. `limited discoverability`
7. `human or trusted-policy review`
8. `stable publication`

Особенности:

- нет baseline;
- нет regression history;
- нет production confidence;
- выше требования к interface review и sandbox trial;
- auto-promotion в `stable` по умолчанию запрещён.

Итого:

- lifecycle framework один;
- validation regime разный.
