# Координация слоёв, роли агентов и единый roadmap

## Зачем нужен этот документ

После трёх предыдущих документов архитектура стала концептуально полной, но остались четыре практических вопроса:

1. Кто именно выполняет мета-когнитивные операции.
2. Как координируются Execution, Control, Knowledge и Meta-Cognitive слои.
3. Как формализовать `DesignTemplate` до уровня реального инстанцирования.
4. Как свести roadmap инфраструктуры и roadmap мета-когнитивного слоя в один план внедрения.

Этот документ закрывает именно эти вопросы.

---

## 1. Модель агентов по ролям

Мета-когнитивные операции не должны выполняться "каким-то общим агентом".

Нужна ролевая специализация.

## 1.1 Базовые роли

### `runtime_agent`

Отвечает только за исполнение конкретной задачи.

Может:

- вызывать systems;
- вызывать capabilities;
- собирать execution traces;
- возвращать output.

Не может:

- публиковать системы;
- менять trusted patterns;
- двигать stable channels.

### `builder_agent`

Отвечает за проектирование и сборку candidate systems.

Может:

- анализировать capability gaps;
- проектировать draft systems;
- создавать candidate versions;
- запускать candidate evaluation;
- предлагать dependency changes.

Не может:

- самостоятельно продвигать stable;
- публиковать trusted patterns;
- утверждать benchmark governance.

### `reflection_agent`

Отвечает за разбор завершённых runs и candidate versions.

Может:

- создавать `ReflectionRecord`;
- формировать lessons learned;
- выявлять anti-pattern signals;
- прикреплять rationale к version history.

Не может:

- менять systems напрямую;
- утверждать pattern promotion;
- изменять release channels.

### `pattern_extraction_agent`

Отвечает за выделение candidate patterns из исторических данных.

Может:

- читать reflection records;
- читать successful case clusters;
- строить `StrategyPattern` draft;
- строить `CapabilityCompositionHypothesis`;
- предлагать design templates.

Не может:

- публиковать pattern как trusted без review;
- менять systems напрямую;
- редактировать benchmark suites.

### `governance_agent`

Отвечает за cross-layer policy checks и подготовку promotion recommendations.

Может:

- проверять budget conflicts;
- проверять semantic drift;
- проверять policy compatibility;
- готовить решение для promotion/rejection.

Не может:

- обходить human gates;
- переписывать history;
- изменять system definition без builder flow.

### `human_reviewer`

Нужен не везде, но обязателен в высокорисковых точках.

Участвует в:

- promotion trusted patterns;
- approval benchmark suites высокого доверия;
- stable promotion при cold-start systems;
- major semantic shifts.

---

## 1.2 Почему роли должны быть разделены

Если `builder_agent` сам:

- проектирует систему;
- сам её оценивает;
- сам рефлексирует;
- сам извлекает паттерн;
- сам утверждает паттерн;

то возникает замкнутый контур самооценки.

Это мета-версия benchmark gaming.

Поэтому минимальное разделение ответственности должно быть таким:

- `runtime_agent` делает;
- `reflection_agent` разбирает;
- `pattern_extraction_agent` абстрагирует;
- `governance_agent` проверяет;
- `human_reviewer` утверждает доверенные переходы.

---

## 1.3 Связь ролей с security model

Роли должны быть встроены в permission model, а не существовать только как концепция.

Пример:

```yaml
permissions:
  roles:
    runtime_agent:
      allow:
        - invoke_system
        - read_registry
        - create_run_trace
    builder_agent:
      allow:
        - create_candidate_system
        - clone_system_version
        - modify_definition
        - run_candidate
    reflection_agent:
      allow:
        - read_run_history
        - create_reflection_record
        - attach_lessons
    pattern_extraction_agent:
      allow:
        - read_reflection_store
        - create_pattern_draft
        - create_template_draft
        - create_composition_hypothesis
    governance_agent:
      allow:
        - evaluate_promotion_request
        - read_all_registries
        - propose_rejection
      deny:
        - mutate_system_definition
    human_reviewer:
      allow:
        - approve_trusted_pattern
        - approve_stable_promotion
        - approve_gold_benchmark_suite
```

---

## 2. Coordination protocol между слоями

Пять-шесть реестров без протокола координации быстро разойдутся по смыслу.

Нужен единый coordination mechanism.

## 2.1 Базовая идея

Каждый значимый переход в системе должен порождать доменное событие.

Слои не должны синхронизироваться через "ручное перечитывание всего".

Правильная модель:

- source of truth хранится в реестрах;
- изменения публикуют events;
- подписчики обновляют производные индексы и представления.

---

## 2.2 Нужен `Domain Event Bus`

Минимальные типы событий:

- `system_created`
- `system_version_created`
- `system_version_promoted`
- `system_version_archived`
- `system_deleted`
- `pattern_draft_created`
- `pattern_promoted`
- `reflection_record_created`
- `task_type_registered`
- `capability_registered`
- `coverage_updated`
- `semantic_drift_detected`
- `budget_exceeded`
- `candidate_rejected`

### Кто публикует события

- `SystemRegistry`
- `PatternRegistry`
- `ReflectionStore`
- `CoverageAnalyzer`
- `GovernanceLayer`

### Кто подписывается

- `CoverageIndex`
- `SemanticDriftMonitor`
- `ConsolidationManager`
- `PatternUsageTracker`
- `BudgetTracker`
- `AuditLog`

---

## 2.3 Источники истины и производные представления

Очень важно не спутать их.

### Sources of truth

- `SystemRegistry`
- `TaskRegistry`
- `CapabilityRegistry`
- `PatternRegistry`
- `ReflectionStore`

### Derived indexes

- `CoverageIndex`
- `SystemLifecycleHealthIndex`
- `PatternUsageIndex`
- `SemanticDriftReport`
- `CapabilityGapView`

Правило:

derived indexes можно пересчитать из sources of truth.

Это защищает от накопления мусора и рассинхрона.

---

## 2.4 Конфликты между слоями

Конфликты неизбежны.

Примеры:

- Coverage говорит "нужна новая система";
- Budget говорит "лимит исчерпан".

Или:

- Builder предлагает сильное расширение;
- SemanticDriftMonitor говорит "система теряет идентичность".

### Нужна единая модель разрешения

Решение не должно приниматься произвольно.

Нужен `GovernanceDecision`.

Пример:

```yaml
decision_id: gov-1
subject: create_new_system
inputs:
  coverage_gap_score: 0.83
  budget_available: false
  semantic_risk: low
decision: defer
rationale:
  - expansion justified by coverage gap
  - blocked by exhausted daily budget
next_action:
  - queue_for_next_window
```

### Возможные решения

- `approve`
- `approve_with_constraints`
- `defer`
- `reject`
- `require_human_review`

---

## 2.5 Минимальный протокол координации

Для начала достаточно такого pipeline:

1. Registry mutation
2. Event emission
3. Derived index updates
4. Governance checks
5. Final state transition or rollback

### Пример

`builder_agent` создаёт candidate system:

1. `SystemRegistry.create_candidate`
2. публикуется `system_version_created`
3. `CoverageIndex` обновляет coverage graph
4. `BudgetTracker` обновляет счётчики
5. `GovernanceAgent` проверяет conflicts
6. если ok — version остаётся active candidate
7. если conflict — candidate переводится в `blocked` или `deferred`

---

## 3. Формализация `DesignTemplate`

Текущая форма templates слишком абстрактна.

Нужен template не в виде "analyzer -> executor -> validator", а в виде parameterized graph schema.

## 3.1 Новый формат template

Пример:

```yaml
template_id: analyzer_executor_validator
title: Analyzer Executor Validator
applies_to_task_types:
  - coding_task
  - test_repair
required_capabilities:
  - analysis
  - execution
  - validation
slots:
  analyzer:
    allowed_node_types:
      - agent_node
      - system_ref_node
    required_capabilities:
      - analysis
    optional_capabilities:
      - decomposition
    cardinality: 1
  executor:
    allowed_node_types:
      - agent_node
      - system_ref_node
    required_capabilities:
      - execution
    cardinality: 1
  validator:
    allowed_node_types:
      - agent_node
      - evaluator_node
      - system_ref_node
    required_capabilities:
      - validation
    cardinality: 1
edges:
  - from_slot: analyzer
    to_slot: executor
    condition_template:
      default: always
  - from_slot: executor
    to_slot: validator
    condition_template:
      default: always
defaults:
  failure_policy:
    on_error: fail_run
instantiation_rules:
  all_required_slots_must_be_bound: true
  node_capability_match_required: true
```

---

## 3.2 Что значит инстанцировать template

Инстанцирование — это не "агент сам догадается".

Это операция:

`DesignTemplate + CapabilityBindings + TaskContext -> DraftSystemDefinition`

### `CapabilityBindings`

Пример:

```yaml
bindings:
  analyzer:
    bind_to: systems.code_analysis_system
  executor:
    bind_to: agents.code_agent
  validator:
    bind_to: systems.test_validation_system
```

### Что проверяет инстанциатор

- все required slots заполнены;
- bound entity реально реализует capability;
- node type допустим для слота;
- edge templates могут быть материализованы;
- interface согласован с task type.

Это уже почти deterministic construction step.

---

## 3.3 Кто инстанцирует template

Для этого нужен отдельный компонент:

- `TemplateInstantiator`

Он не "творит", а делает структурную работу:

- берёт template;
- проверяет bindings;
- строит draft definition;
- возвращает список unresolved constraints.

### Если чего-то не хватает

`TemplateInstantiator` не должен молча фантазировать.

Он должен вернуть:

- `missing_capability_binding`
- `invalid_slot_binding`
- `interface_conflict`

Тогда builder-agent решает, как закрыть пробел.

---

## 4. Pattern extraction pipeline в реалистичной форме

Наивный текст "найти повторяющиеся decision sequences" действительно слишком оптимистичен.

Нужен более приземлённый pipeline.

## 4.1 Какие данные реально нужны

Нужно хранить не только outcome, но и decision traces.

Новая сущность:

- `DecisionTrace`

Пример:

```yaml
trace_id: dt-1
task_type: flaky_test_repair
steps:
  - classify_task
  - choose_pattern:isolate_then_fix
  - choose_template:analyzer_executor_validator
  - bind_executor:code_agent
  - add_validation_step:test_runner
outcome_link:
  reflection_id: refl-123
  system_version: test_repair_system@0.3.0
```

Без `DecisionTrace` pattern extraction почти нечем кормить.

---

## 4.2 Как кластеризовать cases

Кластеры должны строиться не по одному признаку.

Минимально использовать:

- `task_type`
- `selected_pattern`
- `selected_template`
- `outcome_quality_bucket`
- `human_feedback_bucket`

### Outcome buckets

- `high_success`
- `mixed_success`
- `failure`

### Human feedback buckets

- `positive`
- `neutral`
- `negative`
- `missing`

То есть candidate patterns извлекаются не из "всех хороших кейсов вообще", а из структурно похожих решений.

---

## 4.3 Кто валидирует pattern draft

Нужен явный lifecycle pattern promotion.

### Роли

- `pattern_extraction_agent` создаёт draft;
- `governance_agent` проверяет consistency;
- `human_reviewer` утверждает trusted promotion.

### Статусы паттерна

- `draft`
- `candidate`
- `trusted`
- `deprecated`
- `rejected`

### Правило

Ни один автоматически извлечённый паттерн не становится `trusted` без review.

Это особенно важно, потому что trusted patterns будут влиять на будущее проектирование систем.

---

## 5. Единый roadmap

Теперь нужно свести infrastructure roadmap и meta-cognitive roadmap в один.

## Phase 1. System Foundation

Зависимости:

- schemas для systems;
- `SystemRegistry`;
- `SystemVersion`;
- `SystemRelease`;
- basic invoke через proxy mode.

Результат:

- платформа умеет хранить и вызывать versioned systems.

## Phase 2. Controlled Evolution

Зависимости:

- candidate lifecycle;
- evaluation;
- permissions;
- budgets;
- release channels;
- basic governance.

Результат:

- платформа умеет безопасно улучшать и публиковать systems.

## Phase 3. Knowledge Foundation

Зависимости:

- `TaskRegistry`;
- `CapabilityRegistry`;
- `SemanticContract`;
- linking systems to capabilities and task types.

Результат:

- платформа понимает, что за задачи и capabilities у неё есть.

## Phase 4. Pattern Foundation

Зависимости:

- `PatternRegistry`;
- `StrategyPattern`;
- `DesignTemplate`;
- `AntiPattern`;
- `TemplateInstantiator`.

Результат:

- платформа получает reusable cognitive structures.

## Phase 5. Reflection and Traceability

Зависимости:

- `ReflectionStore`;
- `DecisionTrace`;
- `reflection_agent`;
- version/pattern reflection flows.

Результат:

- платформа начинает учиться на процессе, а не только на результате.

## Phase 6. Coverage and Gap Analysis

Зависимости:

- `CoverageIndex`;
- capability-to-task mapping;
- gap detection;
- expansion proposal flow.

Результат:

- self-expansion запускается по обнаруженному разрыву покрытия.

## Phase 7. Pattern Extraction and Composition

Зависимости:

- `pattern_extraction_agent`;
- `CapabilityCompositionHypothesis`;
- clustering of successful cases;
- governance flow for pattern drafts.

Результат:

- платформа начинает извлекать и комбинировать паттерны.

## Phase 8. Drift and Consolidation

Зависимости:

- `SemanticDriftMonitor`;
- `SystemLifecycleHealth`;
- `ConsolidationManager`.

Результат:

- платформа умеет не только расти, но и удерживать смысловую целостность.

---

## 5.1 Матрица зависимостей

Коротко:

- нельзя делать `CoverageIndex` до `SystemRegistry` и `CapabilityRegistry`;
- нельзя делать `PatternExtraction` до `ReflectionStore` и `DecisionTrace`;
- нельзя делать `TemplateInstantiator` до `DesignTemplate` и capability bindings;
- нельзя делать полноценный trusted pattern flow до governance + human review hooks.

---

## 6. Что является MVP

Если делать реально и без перегруза, MVP должен быть таким:

1. `SystemRegistry` + versioned systems
2. proxy invocation
3. candidate lifecycle + permissions
4. `TaskType`, `Capability`, `SemanticContract`
5. `StrategyPattern` + `DesignTemplate`
6. `TemplateInstantiator`
7. `ReflectionRecord`

Это уже даст:

- безопасные systems;
- первичные когнитивные паттерны;
- минимальную рефлексию;
- основу для будущего self-expansion.

А вот автоматический pattern extraction и full consolidation можно делать позже.

---

## 7. Итог

Оставшиеся открытые вопросы действительно уже лежат в implementation design, а не в базовой архитектуре.

Чтобы закрыть их корректно, нужны:

- ролевая модель агентов;
- event-driven coordination protocol;
- инстанцируемые templates;
- реалистичный pattern extraction pipeline;
- единый dependency-aware roadmap.

Только после этого платформу можно будет не просто описывать как самоорганизующуюся, а реально поэтапно строить.
