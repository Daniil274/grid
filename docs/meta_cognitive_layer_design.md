# Мета-когнитивный слой для самоорганизующейся платформы

## Зачем нужен отдельный слой

Текущая архитектура уже задаёт сильный фундамент:

- versioned systems;
- registry;
- lifecycle;
- enforcement;
- bounded APIs;
- self-improvement и self-expansion как контролируемые процессы.

Но этого недостаточно для самоорганизующегося интеллекта.

Эта архитектура хорошо отвечает на вопрос:

- как безопасно создавать, версионировать, публиковать и вызывать системы.

Но она ещё слабо отвечает на вопросы:

- как агент понимает, какой способ решения выбрать;
- как он извлекает удачные стратегии из опыта;
- как переносит стратегию в другой контекст;
- как обнаруживает пробелы в пространстве задач;
- как композиционно выводит новые capabilities;
- как не деградирует семантически при длительной эволюции.

Поэтому нужен отдельный слой:

- не слой исполняемых систем;
- а слой `мета-когнитивных сущностей`.

Это слой про:

- паттерны мышления;
- модель задач;
- рефлексию;
- абстрагирование;
- композицию;
- консолидацию знаний.

---

## 1. Полная архитектура по слоям

Платформу лучше мыслить как 4 уровня.

### Уровень 1. Execution Layer

Исполняемые сущности:

- nodes;
- systems;
- versions;
- runtime;
- release channels.

### Уровень 2. Control Layer

Управление изменением:

- registry;
- lifecycle;
- evaluation;
- permissions;
- budgets;
- canary/promotion.

### Уровень 3. Knowledge Layer

Структурированное знание о:

- задачах;
- capabilities;
- зависимостях;
- design rationale;
- benchmark provenance;
- semantic identity систем.

### Уровень 4. Meta-Cognitive Layer

Сущности более высокого порядка:

- task ontology;
- strategy patterns;
- composition heuristics;
- reflection records;
- pattern library;
- design templates;
- anti-patterns.

Если нет 4 уровня, система умеет строить и менять инструменты, но почти не умеет улучшать способ собственного мышления.

---

## 2. Какие новые сущности нужны

## 2.1 `TaskType`

Первичная сущность пространства задач.

Пример:

```yaml
task_type_id: flaky_test_repair
title: Flaky Test Repair
description: Диагностика и устранение нестабильных тестов
properties:
  requires_root_cause_analysis: true
  requires_reproducibility_check: true
  side_effect_risk: medium
signals:
  - intermittent_failure
  - timing_sensitivity
  - environment_dependency
related_capabilities:
  - test_analysis
  - failure_isolation
  - test_fixing
```

### Зачем это нужно

Без `TaskType` агент не может:

- осмысленно классифицировать задачу;
- делать gap analysis;
- понимать, что новой задаче нужен новый capability cluster.

## 2.2 `Capability`

Capability уже есть в неявном виде, но теперь должна стать типизированной сущностью.

Пример:

```yaml
capability_id: failure_isolation
title: Failure Isolation
inputs:
  - failing_artifact
  - execution_context
outputs:
  - root_cause_hypotheses
quality_dimensions:
  - precision
  - speed
  - explainability
task_types:
  - flaky_test_repair
  - regression_debugging
```

### Важно

Capability должна существовать отдельно от системы.

Система:

- реализует capability;

а capability:

- описывает класс полезного поведения.

## 2.3 `StrategyPattern`

Это недостающее звено между задачей и системой.

Не tool, не system, не benchmark.

Это шаблон мышления.

Пример:

```yaml
pattern_id: isolate_then_fix
title: Isolate Cause Before Repair
applies_to:
  task_types:
    - flaky_test_repair
    - regression_debugging
preconditions:
  - failure_is_reproducible_or_observable
steps:
  - identify failure boundary
  - reduce search space
  - generate hypotheses
  - test hypotheses
  - apply minimal fix
  - revalidate
expected_benefits:
  - lower regression risk
  - better debuggability
anti_patterns:
  - patch_without_isolation
signals_of_success:
  - root_cause_found
  - fix_is_localized
```

### Это и есть "когнитивный инструмент"

Система может быть одной из реализаций паттерна.

Но паттерн сам по себе:

- абстрактнее системы;
- переносим между системами;
- пригоден для reuse при проектировании новых графов.

## 2.4 `ReflectionRecord`

Сущность рефлексии над решением.

Пример:

```yaml
reflection_id: refl-123
task_type: flaky_test_repair
attempted_pattern: isolate_then_fix
system_used: test_repair_system@0.3.0
alternatives_considered:
  - patch_first
  - retry_amplification_then_patch
decision_rationale:
  chosen_because: high_risk_of_false_fix
outcome:
  benchmark_delta: +0.12
  human_feedback: positive
lessons:
  - direct patching was too early
  - adding repro step improved reliability
```

### ReflectionRecord нужен не ради аудита

Он нужен как материал для последующего обучения:

- pattern refinement;
- anti-pattern extraction;
- template generation.

## 2.5 `DesignTemplate`

Шаблон проектирования систем.

Пример:

```yaml
template_id: analyzer_executor_validator
for_task_types:
  - coding_task
  - test_repair
structure:
  nodes:
    - analyzer
    - executor
    - validator
  flow:
    - analyzer -> executor
    - executor -> validator
selection_heuristics:
  use_when:
    - task_requires_preanalysis
    - task_has_verifiable_output
```

Это мост между паттерном и конкретной системой.

## 2.6 `AntiPattern`

Нужно хранить не только полезные паттерны, но и плохие.

Пример:

```yaml
anti_pattern_id: patch_without_isolation
title: Patch Before Understanding
harm:
  - hidden_regressions
  - unstable_fixes
common_contexts:
  - flaky_test_repair
signals:
  - fix_attempt_before_root_cause
```

Без anti-patterns агент будет снова и снова повторять формально допустимые, но плохие решения.

---

## 3. Как связать слой задач, capabilities и паттернов

Нужна явная цепочка.

### Правильная последовательность

`TaskType -> CapabilitySet -> StrategyPattern -> DesignTemplate -> SystemDefinition`

То есть:

1. агент классифицирует задачу как `TaskType`;
2. определяет, какие capabilities нужны;
3. выбирает или выводит подходящий `StrategyPattern`;
4. выбирает `DesignTemplate`;
5. инстанцирует конкретную систему.

Это критически важно.

Без этого агент прыгает прямо из "задача" в "сделаю какой-то граф", а это и есть источник хаотического self-expansion.

---

## 4. Как агент извлекает паттерны из опыта

Паттерны не должны появляться только вручную.

Нужен controlled extraction pipeline.

## 4.1 Источники паттернов

- успешные runs;
- reflection records;
- recurring system structures;
- recurring causal sequences;
- human-authored templates;
- benchmark-backed improvements.

## 4.2 Pipeline извлечения

1. собрать успешные case clusters;
2. сгруппировать по task type;
3. найти повторяющиеся decision sequences;
4. сформировать candidate pattern;
5. связать его с результатами;
6. отправить на validation/review;
7. зарегистрировать в pattern registry.

## 4.3 Что считается паттерном

Не любая повторяемость.

Кандидат в паттерн должен иметь:

- повторяемость;
- переносимость;
- measurable benefit;
- понятные preconditions;
- понятные failure modes.

Иначе это просто локальный трюк, а не паттерн.

---

## 5. Рефлексия как обязательная часть lifecycle

Сейчас evaluation меряет в основном outcome.

Но для мета-обучения нужно мерить ещё и качество принятия решений.

### Поэтому после значимых runs должен существовать этап `reflect`

Он отвечает на вопросы:

- почему был выбран именно этот pattern;
- какие альтернативы рассматривались;
- что было сигналом выбора;
- где произошли ошибки проектирования;
- можно ли обобщить урок.

### Reflection должна быть многоуровневой

#### `Run reflection`

Разбор одного execution.

#### `Version reflection`

Разбор, почему candidate version оказалась лучше или хуже.

#### `Pattern reflection`

Разбор, где паттерн работает, а где нет.

#### `System family reflection`

Разбор группы похожих систем и их semantic drift.

---

## 6. Bootstrapping problem

Проблема курицы и яйца реальна:

чтобы проектировать системы, агент уже должен обладать design competence.

Значит нужен bootstrap слой.

## 6.1 Источники bootstrap-компетенции

- reference systems;
- design templates;
- curated strategy patterns;
- annotated design rationale;
- worked examples;
- anti-pattern library.

## 6.2 Что должно храниться для bootstrap

Не только готовые системы, но и:

- почему система устроена так;
- какие alternatives были отвергнуты;
- при каких task types этот дизайн работает;
- какие risks в нём типичны.

### Минимальный bootstrap package

```yaml
bootstrap_knowledge:
  reference_systems:
    - coding_assistant
    - document_analysis
  strategy_patterns:
    - analyzer_executor_validator
    - isolate_then_fix
  anti_patterns:
    - patch_without_isolation
  design_templates:
    - planner_executor_verifier
```

Это позволяет новому builder-agent не изобретать архитектуру с нуля на каждом шаге.

---

## 7. Capability composition

Discovery capability — это только половина задачи.

Нужен механизм композиции capabilities в новые capability clusters.

## 7.1 Базовая идея

Новая система часто рождается не из новой атомарной capability, а из новой комбинации старых.

Пример:

- `code_analysis`
- `test_generation`
- `execution_feedback`

могут вместе образовать:

- `mutation_testing`.

## 7.2 Нужна сущность `CapabilityCompositionHypothesis`

Пример:

```yaml
hypothesis_id: capcomp-1
inputs:
  - code_analysis
  - test_generation
  - execution_feedback
proposed_emergent_capability: mutation_testing
rationale:
  - together they can generate and assess mutation-based robustness
confidence: 0.62
status: draft
```

### Жизненный цикл гипотезы

1. обнаружить сочетание capabilities;
2. выдвинуть гипотезу emergent capability;
3. предложить prototype system;
4. проверить на task cluster;
5. подтвердить или отклонить.

Это и есть формализация "творческой композиции".

---

## 8. Task ontology как основа gap analysis

Self-expansion должно начинаться не с "давайте построим систему", а с анализа разрыва.

### Для этого нужны три пространства

#### `Task Space`

Какие типы задач вообще существуют.

#### `Capability Space`

Какие виды полезного поведения умеет платформа.

#### `Coverage Map`

Какие task types покрываются какими capabilities и системами.

### Пример coverage record

```yaml
task_type: flaky_test_repair
required_capabilities:
  - failure_isolation
  - test_fixing
  - validation
covered_by:
  - test_repair_system@0.3.0
coverage_score: 0.74
known_gaps:
  - weak_environment_modeling
```

### Тогда self-expansion запускается по правилу

Если:

- задача относится к известному `TaskType`;
- coverage недостаточен;
- нужный capability cluster отсутствует;

тогда:

- создаётся expansion proposal.

Это намного лучше, чем ad hoc генерация новых систем.

---

## 9. Semantic drift

Это один из самых недооценённых рисков.

Система может сохранять `system_id`, но перестать быть тем, чем была.

Нужен механизм semantic identity.

## 9.1 `SemanticContract`

Для каждой системы должна существовать сущность:

```yaml
system_id: coding_assistant
semantic_contract:
  primary_task_types:
    - coding_task
  primary_capabilities:
    - code_generation
    - refactoring
    - code_explanation
  forbidden_drift:
    - becomes_general_web_research_system
```

## 9.2 Drift detection

Проверять:

- distribution задач, на которых система теперь успешна;
- изменение exported capabilities;
- изменение output style/shape;
- изменение dependency profile;
- изменение benchmark portfolio.

### Если drift превышает порог

Варианты:

- require major version bump;
- require rename / fork into new system;
- block promotion until reviewed.

Без этого self-improvement может незаметно превратиться в смену назначения системы.

---

## 10. Combinatorial explosion

Budget — это только throttling.

Нужны ещё механизмы сокращения сложности.

## 10.1 Нужна сущность `SystemLifecycleHealth`

Пример:

```yaml
system_id: test_repair_system
usage_frequency: low
dependency_count: 0
last_successful_use: 2026-03-01
duplication_score: 0.81
semantic_overlap_with:
  - flaky_debug_system
recommended_action: consolidate
```

## 10.2 Политики сокращения

- archive unused systems;
- merge semantically overlapping systems;
- deprecate low-value candidates;
- collapse system families into templates/patterns;
- promote pattern reuse over new system creation.

### Правило

Self-expansion должна сопровождаться `self-consolidation`.

Иначе платформа будет только расти, но не организовываться.

---

## 11. Evaluation gaming и Goodhart's Law

Даже хороший benchmark governance не решает проблему полностью.

Нужны дополнительные защитные механизмы.

## 11.1 Многомерная оценка

Нельзя оптимизировать только один score.

Нужны измерения:

- usefulness;
- robustness;
- transferability;
- explainability;
- cost efficiency;
- human satisfaction;
- long-term regression stability.

## 11.2 Hidden evaluation

Часть suites должна быть невидима builder-agent.

Иначе оптимизация пойдёт в benchmark gaming.

## 11.3 Delayed evaluation

Нужна оценка не только сразу после candidate, но и позже:

- post-promotion performance;
- downstream system effects;
- user correction rate;
- rollback frequency.

## 11.4 Pattern-level evaluation

Оценивать надо не только системы, но и паттерны.

Например:

- паттерн `patch_first` может давать быстрый локальный gain;
- но ухудшать долгосрочную стабильность.

---

## 12. Pattern Registry

Нужен отдельный реестр, не смешанный с SystemRegistry.

### Он хранит

- strategy patterns;
- anti-patterns;
- design templates;
- composition hypotheses;
- reflection-derived lessons;
- pattern usage stats;
- pattern confidence scores.

### Почему отдельно

Потому что:

- system registry хранит исполняемые артефакты;
- pattern registry хранит абстракции и мета-знание.

Это разные типы сущностей и разные lifecycle.

---

## 13. Как всё связывается вместе

Полный цикл должен выглядеть так:

1. Приходит новая задача.
2. Агент классифицирует её в `TaskType`.
3. Смотрит coverage map.
4. Если покрытия хватает:
   выбирает подходящий `StrategyPattern`.
5. Если покрытия не хватает:
   формирует `CapabilityGap`.
6. На основе gap и pattern/template предлагает:
   - новую систему;
   - или расширение существующей.
7. Создаёт candidate version/system.
8. Запускает evaluation.
9. Выполняет reflection.
10. Обновляет:
   - pattern registry;
   - task coverage;
   - design templates;
   - anti-pattern library.

Только такой цикл превращает платформу в реально обучающуюся.

---

## 14. Новые реестры и модули

Помимо `SystemRegistry` нужны ещё:

- `TaskRegistry`
- `CapabilityRegistry`
- `PatternRegistry`
- `ReflectionStore`
- `CoverageIndex`

### Возможная структура кода

```text
core/
  task_ontology.py
  capability_registry.py
  pattern_registry.py
  reflection_engine.py
  coverage_analyzer.py
  semantic_drift_monitor.py
  consolidation_manager.py

schemas/
  task_type.py
  capability.py
  strategy_pattern.py
  reflection_record.py
  design_template.py
  anti_pattern.py
  capability_composition.py
  semantic_contract.py
```

---

## 15. Минимальный путь внедрения

Чтобы не перегрузить проект, это тоже нужно внедрять поэтапно.

### Этап M1

Добавить:

- `TaskType`
- `Capability`
- `SemanticContract`

И связать их с системами.

### Этап M2

Добавить:

- `StrategyPattern`
- `DesignTemplate`
- `AntiPattern`

и ручной pattern registry.

### Этап M3

Добавить:

- `ReflectionRecord`
- reflection pipeline после evaluation;
- pattern usage metrics.

### Этап M4

Добавить:

- `CoverageIndex`
- `CapabilityCompositionHypothesis`
- gap-driven self-expansion.

### Этап M5

Добавить:

- semantic drift monitor;
- consolidation manager;
- pattern-level evaluation.

---

## 16. Итог

Чтобы получилась не просто безопасная платформа систем, а самоорганизующийся интеллект, нужно явно разделить:

- исполнение;
- управление изменением;
- знание;
- мета-когницию.

Тогда:

- systems будут исполняемыми артефактами;
- patterns будут когнитивными стратегиями;
- task ontology даст пространство для gap analysis;
- reflection даст улучшение не только outputs, но и способов мышления;
- capability composition даст источник новых систем;
- semantic drift и consolidation не дадут платформе расползтись и потерять смысл.

Именно этот слой превращает “платформу, которая умеет строить новые инструменты” в “платформу, которая умеет учиться строить лучшие способы мышления и лучшие инструменты”.
