# Архитектура агентских систем как исполняемых нод

## Зачем это нужно

Сейчас в Grid уже есть сильные заготовки:

- конфиг-ориентированное описание агентов и инструментов;
- dynamic agents через `AgentFactory`;
- system tools для интроспекции;
- improvement loop с registry, experiment, evaluation и promotion;
- pipeline/runtime слой для сериализованного исполнения.

Но онтология пока разорвана:

- `agents` есть как первичная сущность;
- `agent systems` фактически существуют, но не оформлены как объект модели;
- self-improvement живёт рядом с runtime, а не внутри общей архитектуры;
- версия системы, эксперимент и релиз не являются первоклассными сущностями.

Из-за этого саморазвитие выглядит как надстройка над конфигом, а не как нормальная часть платформы.

## Главная идея

Нужно поднять уровень абстракции:

- не `agent` как единственная исполняемая сущность;
- а `node` как общий исполняемый объект;
- где `agent system config` компилируется в `system node`.

Тогда:

- агент остаётся частным случаем ноды;
- целая агентская система тоже становится нодой;
- системные агенты могут видеть не только отдельных агентов, но и целые системы;
- версии систем, эксперименты и тестовые прогоны становятся естественной частью runtime.

## Базовая модель

### 1. Node

Единая сущность исполнения.

Типы нод:

- `agent` — один агент с моделью, инструментами и prompt;
- `system` — композиция из нескольких нод с входной точкой;
- `tool` — внешняя или встроенная функция;
- `workflow` — декларативный граф шагов;
- `router` — выбор следующей ноды;
- `evaluator` — проверка результата;
- `benchmark` — сценарий оценки;
- `policy` — правила доступа, лимитов и promotion.

### 2. System Definition

Описание агентской системы как графа.

Обязательные свойства:

- `id`;
- `version`;
- `entrypoint`;
- `default_agent`;
- `nodes`;
- `interfaces`;
- `policies`;
- `dependencies`.

Смысл:

- `default_agent` нужен для обратной совместимости и простого запуска;
- `entrypoint` нужен для корректной модели исполнения;
- `nodes` задают внутренний граф;
- `interfaces` определяют, как система вызывается снаружи;
- `dependencies` позволяют системе использовать другие системы.

### 3. System Version

Версия системы должна быть неизменяемой.

Это ключевой принцип, иначе self-improvement превращается в перетирание живого конфига.

Правильная модель:

- definition immutable;
- release pointers mutable.

То есть меняется не существующая версия, а указатель:

- `stable`;
- `candidate`;
- `experiment`;
- `canary`;
- `archived`.

### 4. System Registry

Нужен отдельный реестр систем, а не только improvement registry.

Он хранит:

- список систем;
- их версии;
- активные alias/каналы;
- зависимости между системами;
- контракты интерфейсов;
- историю публикаций;
- связи с экспериментами и benchmark-результатами.

## Целевая архитектура

### Слой 1. Declarative Layer

Конфиг описывает не только агентов и tools, но и системы.

Примерно так:

```yaml
systems:
  coding_assistant:
    title: Coding Assistant
    description: Система для инженерных задач
    entrypoint: main
    default_agent: chat_agent
    versioning:
      strategy: immutable
      channel: stable
    interfaces:
      invoke:
        input_schema: schemas/system_input/coding_task.json
        output_schema: schemas/system_output/coding_result.json
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
      docs:
        type: agent
        ref: agents.doc_analyzer
    edges:
      - from: main
        to: planner
        when: task.is_complex == true
      - from: planner
        to: coder
        when: step.kind == "implementation"
    exports:
      capabilities: [code, planning, docs]
      tags: [engineering]
    policies:
      isolation_profile: default
      evaluation_profile: coding_regression_suite
```

### Слой 2. Compilation Layer

Конфиг не должен исполняться напрямую как YAML на каждом шаге.

Нужен компилятор:

- `config -> normalized IR -> executable graph`.

IR должен быть единым и для одиночного агента, и для целой системы.

Это убирает костыли, потому что runtime работает не с разными сущностями, а с одной моделью графа.

### Слой 3. Runtime Layer

Runtime должен уметь запускать любую ноду по одинаковому контракту:

- `invoke(node_ref, input, context, policy) -> result`.

Тогда:

- агент вызывает систему так же, как другую ноду;
- система вызывает вложенную систему тем же способом;
- self-improvement агент может запускать candidate-версии как обычные callable nodes.

### Слой 4. Registry Layer

Нужно разделить два реестра:

- `SystemRegistry` — что существует;
- `ImprovementRegistry` — что меняем и как проверяем.

Связь между ними такая:

- проблема относится к системе или версии системы;
- эксперимент создаёт candidate version;
- evaluator проверяет candidate;
- promotion двигает alias канала на новую версию.

### Слой 5. Evaluation Layer

Оцениваться должен не просто `config_diff`, а исполняемая версия системы.

Сценарий:

1. есть `system@1.4.0`;
2. агент создаёт `system@1.5.0-candidate.3`;
3. benchmark гоняется именно по candidate version;
4. canary идёт на alias `canary`;
5. после успеха `stable -> 1.5.0`.

Это намного чище, чем “изменили кусок конфига и как-то применили”.

## Как именно превратить агентскую систему в ноду

### Правило

Любая система обязана иметь внешний контракт вызова.

Минимальный контракт:

- `system_id`;
- `version_selector`;
- `input`;
- `execution_mode`;
- `constraints`.

Пример вызова:

```json
{
  "system_id": "coding_assistant",
  "version_selector": "stable",
  "input": {
    "task": "Исправь flaky tests вокруг config proposer"
  },
  "execution_mode": "sync"
}
```

Ответ:

```json
{
  "run_id": "run-123",
  "system_id": "coding_assistant",
  "resolved_version": "1.5.0",
  "status": "completed",
  "output": {
    "summary": "Tests stabilized"
  },
  "artifacts": [],
  "metrics": {}
}
```

Тогда системный агент может:

- получить список систем;
- запросить интерфейс системы;
- вызвать систему;
- вызвать конкретную версию;
- сравнить несколько версий;
- запустить benchmark;
- выпустить candidate.

## Какие сущности нужны в модели данных

### `SystemDefinition`

- `id`
- `title`
- `description`
- `entrypoint`
- `default_agent`
- `nodes`
- `edges`
- `interfaces`
- `exports`
- `policies`
- `dependencies`

### `SystemVersion`

- `system_id`
- `version`
- `definition_hash`
- `created_at`
- `created_by`
- `source_experiment_id`
- `parent_version`
- `status`

### `SystemRelease`

- `system_id`
- `channel`
- `version`
- `rollout`
- `updated_at`

### `SystemExperiment`

Можно либо расширить текущий `ImprovementExperiment`, либо сделать связанный слой поверх него.

Главное, чтобы эксперимент ссылался не на абстрактный diff, а на:

- `target_system_id`
- `base_version`
- `candidate_version`
- `change_set`
- `evaluation_suite`

## Предлагаемая структура конфигов и файлов

Если делать чисто, лучше уйти от одного огромного `config.yaml` к каталогам.

Пример:

```text
config/
  agents/
    chat_agent.yaml
    code_agent.yaml
    coordinator.yaml
  systems/
    coding_assistant/
      system.yaml
      versions/
        1.0.0.yaml
        1.1.0.yaml
    memory_system/
      system.yaml
  policies/
    default.yaml
    safe_evolution.yaml
  benchmarks/
    coding_regression.yaml
  releases/
    systems.yaml
```

Почему так лучше:

- системы отделены от агентов;
- версия системы хранится явно;
- легче диффить и сравнивать;
- проще делать candidate и canary;
- можно хранить несколько параллельных вариантов без засорения главного файла.

## Что должен уметь системный агент

Не прямой доступ к live config, а bounded capability surface.

Нужны инструменты уровня системы:

- `system_list_systems`
- `system_get_system_info`
- `system_get_system_versions`
- `system_invoke_system`
- `system_compare_versions`
- `system_create_candidate_version`
- `system_run_benchmark`
- `system_promote_version`
- `system_archive_version`

Важно:

агент не должен “править конфиг как текст”.

Он должен работать через операции домена:

- создать версию;
- изменить ноду;
- изменить dependency;
- переключить entrypoint;
- сменить policy;
- запустить оценку.

Это главный анти-костыльный принцип.

## Как встроить self-improvement без мёртвого кода

### Плохой путь

- отдельный self-improvement режим;
- отдельные special-case функции;
- прямое редактирование текущего `config.yaml`;
- promotion как побочный эффект.

### Хороший путь

self-improvement — это обычный consumer платформы системных нод.

То есть улучшатель работает так:

1. наблюдает проблему;
2. выбирает target system;
3. создаёт candidate version;
4. меняет graph definition через domain API;
5. запускает benchmark;
6. запускает canary;
7. двигает release channel.

Это не отдельная “магия”, а обычный lifecycle любой системы.

## Взаимодействие с текущим кодом Grid

На базе текущего репо это раскладывается так.

### Уже можно переиспользовать

- `core/agent_factory.py` как runtime для agent nodes;
- `tools/system_tools.py` как основу интроспекции;
- `tools/orchestrator_tools.py` как механизм исполнения динамических графов;
- `core/pipeline_registry.py` как execution coordination;
- `core/improvement_registry.py` как основу experiment lifecycle;
- `core/evaluator.py` и `benchmarks/run.py` как evaluation layer.

### Чего не хватает

- `SystemRegistry`;
- `SystemDefinition` / `SystemVersion` schemas;
- compiler `config -> graph IR`;
- system-level tool surface;
- release/channel management;
- version-aware invocation.

## Как это должно выглядеть структурно в коде

```text
core/
  system_registry.py
  system_compiler.py
  system_runtime.py
  system_release_manager.py
  system_experiment_manager.py

schemas/
  system_definition.py
  system_version.py

tools/
  system_registry_tools.py
  system_runtime_tools.py
  system_release_tools.py
```

## Правильный жизненный цикл

### 1. Authoring

Система описывается декларативно.

### 2. Compile

Конфиг компилируется в нормализованный graph IR.

### 3. Register

Версия регистрируется как неизменяемый артефакт.

### 4. Invoke

Система вызывается по контракту через runtime.

### 5. Observe

Логи, метрики и результаты привязываются к версии системы.

### 6. Experiment

На основе версии создаётся candidate.

### 7. Evaluate

Прогоняются benchmark/canary.

### 8. Promote

Меняется alias канала, а не содержимое старой версии.

## Ключевые принципы, чтобы не было костылём

1. Одна исполняемая абстракция: всё является `node`.
2. Система — это не особый режим, а обычная callable node.
3. Версии immutable, каналы mutable.
4. Изменения только через domain operations, не через редактирование YAML как текста.
5. Evaluation работает по версиям систем, не по “идеям изменений”.
6. Runtime ничего не знает о self-improvement как об исключении.
7. Интроспекция и управление идут через tool/API surface, а не через прямой доступ к внутренностям.

## Рекомендуемая последовательность внедрения

### Этап 1

Добавить `systems:` как новую секцию конфига и сделать `SystemRegistry` без изменения runtime.

### Этап 2

Сделать `system_invoke_system`, где system пока просто проксируется в `default_agent`.

Это даст совместимость и быстрый старт.

### Этап 3

Добавить graph IR и `system_runtime`, чтобы система стала полноценной композицией нод.

### Этап 4

Перевести improvement loop с `config_diff` на `candidate system version`.

### Этап 5

Вынести version/channel/release в отдельный слой и подключить canary/promotion.

## Короткий вывод

Правильная цель не в том, чтобы “агент редактировал конфиг и улучшал себя”.

Правильная цель в том, чтобы:

- конфиг описывал исполняемые системы;
- система была нодой;
- нода имела версию и контракт;
- агенты могли вызывать и сравнивать системы;
- improvement loop управлял версиями и релизами систем.

Тогда саморазвитие получится не магическим и не хрупким, а естественным свойством платформы.
