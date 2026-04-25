# Meta-Cognitive Layer for a Self-Organizing Platform

## Why a Separate Layer Is Needed

The current architecture already provides a strong foundation:

- versioned systems;
- registry;
- lifecycle;
- enforcement;
- bounded APIs;
- self-improvement and self-expansion as controlled processes.

But this is not enough for a self-organizing intelligence.

This architecture answers well the question:

- how to safely create, version, publish and invoke systems.

But it still weakly answers the questions:

- how does an agent understand which solution method to choose;
- how does it extract successful strategies from experience;
- how does it transfer a strategy to another context;
- how does it detect gaps in the task space;
- how does it compositionally derive new capabilities;
- how does it not degrade semantically during long-term evolution.

Therefore, a separate layer is needed:

- not a layer of executable systems;
- but a layer of `meta-cognitive entities`.

This layer is about:

- thinking patterns;
- task model;
- reflection;
- abstraction;
- composition;
- knowledge consolidation.

---

## 1. Complete Architecture by Layers

The platform is best thought of as 4 levels.

### Level 1. Execution Layer

Executable entities:

- nodes;
- systems;
- versions;
- runtime;
- release channels.

### Level 2. Control Layer

Change management:

- registry;
- lifecycle;
- evaluation;
- permissions;
- budgets;
- canary/promotion.

### Level 3. Knowledge Layer

Structured knowledge about:

- tasks;
- capabilities;
- dependencies;
- design rationale;
- benchmark provenance;
- semantic identity of systems.

### Level 4. Meta-Cognitive Layer

Higher-order entities:

- task ontology;
- strategy patterns;
- composition heuristics;
- reflection records;
- pattern library;
- design templates;
- anti-patterns.

Without level 4, the system can build and change tools, but almost cannot improve its own way of thinking.

---

## 2. What New Entities Are Needed

## 2.1 `TaskType`

Primary entity of the task space.

Example:

```yaml
task_type_id: flaky_test_repair
title: Flaky Test Repair
description: Diagnosis and elimination of unstable tests
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

### Why This Is Needed

Without `TaskType`, an agent cannot:

- meaningfully classify a task;
- do gap analysis;
- understand that a new task needs a new capability cluster.

## 2.2 `Capability`

Capability already exists implicitly, but now should become a typed entity.

Example:

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

### Important

Capability should exist separately from a system.

A system:

- implements a capability;

while a capability:

- describes a class of useful behavior.

## 2.3 `StrategyPattern`

This is the missing link between task and system.

Not a tool, not a system, not a benchmark.

It is a thinking template.

Example:

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

### This Is a "Cognitive Tool"

A system can be one of the implementations of a pattern.

But the pattern itself:

- is more abstract than a system;
- is portable between systems;
- is suitable for reuse when designing new graphs.

## 2.4 `ReflectionRecord`

Entity for reflection on a solution.

Example:

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

### ReflectionRecord Is Not Just for Audit

It is needed as material for subsequent learning:

- pattern refinement;
- anti-pattern extraction;
- template generation.

## 2.5 `DesignTemplate`

System design template.

Example:

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

This is a bridge between a pattern and a concrete system.

## 2.6 `AntiPattern`

Not only useful patterns should be stored, but also bad ones.

Example:

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

Without anti-patterns, the agent will repeatedly make formally acceptable but poor decisions.

---

## 3. How to Connect Task Layer, Capabilities and Patterns

An explicit chain is needed.

### Correct Sequence

`TaskType -> CapabilitySet -> StrategyPattern -> DesignTemplate -> SystemDefinition`

That is:

1. the agent classifies the task as a `TaskType`;
2. determines what capabilities are needed;
3. selects or derives a suitable `StrategyPattern`;
4. selects a `DesignTemplate`;
5. instantiates a concrete system.

This is critically important.

Without this, the agent jumps directly from "task" to "I'll make some graph", which is the source of chaotic self-expansion.

---

## 4. How an Agent Extracts Patterns from Experience

Patterns should not appear only manually.

A controlled extraction pipeline is needed.

## 4.1 Sources of Patterns

- successful runs;
- reflection records;
- recurring system structures;
- recurring causal sequences;
- human-authored templates;
- benchmark-backed improvements.

## 4.2 Extraction Pipeline

1. collect successful case clusters;
2. group by task type;
3. find recurring decision sequences;
4. form a candidate pattern;
5. link it to results;
6. send for validation/review;
7. register in pattern registry.

## 4.3 What Counts as a Pattern

Not any recurrence.

A pattern candidate must have:

- reproducibility;
- portability;
- measurable benefit;
- clear preconditions;
- clear failure modes.

Otherwise, it is just a local trick, not a pattern.

---

## 5. Reflection as a Mandatory Part of the Lifecycle

Currently, evaluation mainly measures outcome.

But for meta-learning, the quality of decision-making also needs to be measured.

### Therefore, after significant runs, there should be a `reflect` stage

It answers the questions:

- why was this particular pattern chosen;
- what alternatives were considered;
- what was the signal for the choice;
- where did design errors occur;
- can the lesson be generalized.

### Reflection Should Be Multi-Level

#### `Run reflection`

Analysis of a single execution.

#### `Version reflection`

Analysis of why a candidate version turned out better or worse.

#### `Pattern reflection`

Analysis of where a pattern works and where it does not.

#### `System family reflection`

Analysis of a group of similar systems and their semantic drift.

---

## 6. Bootstrapping Problem

The chicken-and-egg problem is real:

to design systems, an agent must already possess design competence.

Therefore, a bootstrap layer is needed.

## 6.1 Sources of Bootstrap Competence

- reference systems;
- design templates;
- curated strategy patterns;
- annotated design rationale;
- worked examples;
- anti-pattern library.

## 6.2 What Should Be Stored for Bootstrap

Not only ready-made systems, but also:

- why the system is structured that way;
- what alternatives were rejected;
- under what task types this design works;
- what risks are typical for it.

### Minimal Bootstrap Package

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

This allows a new builder-agent not to invent architecture from scratch at every step.

---

## 7. Capability Composition

Discovery of a capability is only half the task.

A mechanism for composing capabilities into new capability clusters is needed.

## 7.1 Basic Idea

A new system is often born not from a new atomic capability, but from a new combination of old ones.

Example:

- `code_analysis`
- `test_generation`
- `execution_feedback`

can together form:

- `mutation_testing`.

## 7.2 An Entity `CapabilityCompositionHypothesis` Is Needed

Example:

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

### Hypothesis Lifecycle

1. detect a combination of capabilities;
2. propose an emergent capability hypothesis;
3. propose a prototype system;
4. test on a task cluster;
5. confirm or reject.

This is the formalization of "creative composition".

---

## 8. Task Ontology as a Basis for Gap Analysis

Self-expansion should start not with "let's build a system", but with gap analysis.

### Three Spaces Are Needed

#### `Task Space`

What types of tasks exist in general.

#### `Capability Space`

What types of useful behavior the platform can perform.

#### `Coverage Map`

Which task types are covered by which capabilities and systems.

### Example Coverage Record

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

### Then Self-Expansion Is Triggered by the Rule

If:

- the task belongs to a known `TaskType`;
- coverage is insufficient;
- the required capability cluster is missing;

then:

- an expansion proposal is created.

This is much better than ad hoc generation of new systems.

---

## 9. Semantic Drift

This is one of the most underestimated risks.

A system may keep its `system_id`, but cease to be what it was.

A semantic identity mechanism is needed.

## 9.1 `SemanticContract`

For each system, there should be an entity:

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

## 9.2 Drift Detection

Check:

- distribution of tasks on which the system is now successful;
- change in exported capabilities;
- change in output style/shape;
- change in dependency profile;
- change in benchmark portfolio.

### If Drift Exceeds a Threshold

Options:

- require major version bump;
- require rename / fork into new system;
- block promotion until reviewed.

Without this, self-improvement can imperceptibly transform into a change of the system's purpose.

---

## 10. Combinatorial Explosion

Budget is only throttling.

Mechanisms for reducing complexity are also needed.

## 10.1 An Entity `SystemLifecycleHealth` Is Needed

Example:

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

## 10.2 Reduction Policies

- archive unused systems;
- merge semantically overlapping systems;
- deprecate low-value candidates;
- collapse system families into templates/patterns;
- promote pattern reuse over new system creation.

### Rule

Self-expansion must be accompanied by `self-consolidation`.

Otherwise, the platform will only grow, but not organize itself.

---

## 11. Evaluation Gaming and Goodhart's Law

Even good benchmark governance does not fully solve the problem.

Additional protective mechanisms are needed.

## 11.1 Multi-Dimensional Evaluation

One cannot optimize only a single score.

Measurements needed:

- usefulness;
- robustness;
- transferability;
- explainability;
- cost efficiency;
- human satisfaction;
- long-term regression stability.

## 11.2 Hidden Evaluation

Part of the suites should be invisible to the builder-agent.

Otherwise, optimization will go into benchmark gaming.

## 11.3 Delayed Evaluation

Evaluation is needed not only immediately after a candidate, but also later:

- post-promotion performance;
- downstream system effects;
- user correction rate;
- rollback frequency.

## 11.4 Pattern-Level Evaluation

Not only systems but also patterns should be evaluated.

For example:

- the `patch_first` pattern may give quick local gain;
- but worsen long-term stability.

---

## 12. Pattern Registry

A separate registry is needed, not mixed with SystemRegistry.

### It Stores

- strategy patterns;
- anti-patterns;
- design templates;
- composition hypotheses;
- reflection-derived lessons;
- pattern usage stats;
- pattern confidence scores.

### Why Separate

Because:

- system registry stores executable artifacts;
- pattern registry stores abstractions and meta-knowledge.

These are different types of entities with different lifecycles.

---

## 13. How Everything Connects Together

The complete cycle should look like this:

1. A new task arrives.
2. The agent classifies it into a `TaskType`.
3. Checks the coverage map.
4. If coverage is sufficient:
   selects a suitable `StrategyPattern`.
5. If coverage is insufficient:
   forms a `CapabilityGap`.
6. Based on the gap and pattern/template, proposes:
   - a new system;
   - or an extension of an existing one.
7. Creates a candidate version/system.
8. Runs evaluation.
9. Performs reflection.
10. Updates:
   - pattern registry;
   - task coverage;
   - design templates;
   - anti-pattern library.

Only such a cycle turns the platform into a truly learning one.

---

## 14. New Registries and Modules

In addition to `SystemRegistry`, the following are needed:

- `TaskRegistry`
- `CapabilityRegistry`
- `PatternRegistry`
- `ReflectionStore`
- `CoverageIndex`

### Possible Code Structure

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

## 15. Minimal Implementation Path

To avoid overloading the project, this should also be implemented in stages.

### Stage M1

Add:

- `TaskType`
- `Capability`
- `SemanticContract`

And link them to systems.

### Stage M2

Add:

- `StrategyPattern`
- `DesignTemplate`
- `AntiPattern`

and a manual pattern registry.

### Stage M3

Add:

- `ReflectionRecord`
- reflection pipeline after evaluation;
- pattern usage metrics.

### Stage M4

Add:

- `CoverageIndex`
- `CapabilityCompositionHypothesis`
- gap-driven self-expansion.

### Stage M5

Add:

- semantic drift monitor;
- consolidation manager;
- pattern-level evaluation.

---

## 16. Summary

To achieve not just a safe platform of systems, but a self-organizing intelligence, we need to explicitly separate:

- execution;
- change management;
- knowledge;
- meta-cognition.

Then:

- systems will be executable artifacts;
- patterns will be cognitive strategies;
- task ontology will provide a space for gap analysis;
- reflection will provide improvement not only of outputs but also of ways of thinking;
- capability composition will provide a source of new systems;
- semantic drift and consolidation will prevent the platform from sprawling and losing its meaning.

It is this layer that turns a "platform that can build new tools" into a "platform that can learn to build better ways of thinking and better tools".
