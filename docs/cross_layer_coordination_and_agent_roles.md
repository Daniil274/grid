# Cross-Layer Coordination, Agent Roles and Unified Roadmap

## Why This Document Is Needed

After the three previous documents, the architecture became conceptually complete, but four practical questions remained:

1. Who exactly performs meta-cognitive operations.
2. How the Execution, Control, Knowledge and Meta-Cognitive layers coordinate.
3. How to formalize `DesignTemplate` to the level of actual instantiation.
4. How to merge the infrastructure roadmap and the meta-cognitive layer roadmap into one implementation plan.

This document addresses exactly these questions.

---

## 1. Agent Model by Roles

Meta-cognitive operations should not be performed by "some generic agent".

Role-based specialization is needed.

## 1.1 Base Roles

### `runtime_agent`

Responsible only for executing a specific task.

Can:

- call systems;
- call capabilities;
- collect execution traces;
- return output.

Cannot:

- publish systems;
- change trusted patterns;
- move stable channels.

### `builder_agent`

Responsible for designing and assembling candidate systems.

Can:

- analyze capability gaps;
- design draft systems;
- create candidate versions;
- run candidate evaluation;
- propose dependency changes.

Cannot:

- independently promote stable;
- publish trusted patterns;
- approve benchmark governance.

### `reflection_agent`

Responsible for analyzing completed runs and candidate versions.

Can:

- create `ReflectionRecord`;
- formulate lessons learned;
- identify anti-pattern signals;
- attach rationale to version history.

Cannot:

- change systems directly;
- approve pattern promotion;
- modify release channels.

### `pattern_extraction_agent`

Responsible for extracting candidate patterns from historical data.

Can:

- read reflection records;
- read successful case clusters;
- build `StrategyPattern` draft;
- build `CapabilityCompositionHypothesis`;
- propose design templates.

Cannot:

- publish a pattern as trusted without review;
- change systems directly;
- edit benchmark suites.

### `governance_agent`

Responsible for cross-layer policy checks and preparing promotion recommendations.

Can:

- check budget conflicts;
- check semantic drift;
- check policy compatibility;
- prepare decision for promotion/rejection.

Cannot:

- bypass human gates;
- rewrite history;
- change system definition without builder flow.

### `human_reviewer`

Not needed everywhere, but mandatory at high-risk points.

Participates in:

- promotion of trusted patterns;
- approval of high-trust benchmark suites;
- stable promotion for cold-start systems;
- major semantic shifts.

---

## 1.2 Why Roles Should Be Separate

If `builder_agent` itself:

- designs the system;
- evaluates it itself;
- reflects on it itself;
- extracts the pattern itself;
- approves the pattern itself;

then a closed self-assessment loop emerges.

This is a meta-version of benchmark gaming.

Therefore, the minimum separation of responsibilities should be:

- `runtime_agent` does;
- `reflection_agent` analyzes;
- `pattern_extraction_agent` abstracts;
- `governance_agent` checks;
- `human_reviewer` approves trusted transitions.

---

## 1.3 Relationship of Roles to Security Model

Roles must be embedded in the permission model, not exist only as a concept.

Example:

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

## 2. Coordination Protocol Between Layers

Five to six registries without a coordination protocol will quickly diverge in meaning.

A unified coordination mechanism is needed.

## 2.1 Basic Idea

Every significant transition in the system should generate a domain event.

Layers should not synchronize via "manual re-reading of everything".

The correct model:

- source of truth is stored in registries;
- changes publish events;
- subscribers update derived indexes and views.

---

## 2.2 A `Domain Event Bus` Is Needed

Minimum event types:

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

### Who Publishes Events

- `SystemRegistry`
- `PatternRegistry`
- `ReflectionStore`
- `CoverageAnalyzer`
- `GovernanceLayer`

### Who Subscribes

- `CoverageIndex`
- `SemanticDriftMonitor`
- `ConsolidationManager`
- `PatternUsageTracker`
- `BudgetTracker`
- `AuditLog`

---

## 2.3 Sources of Truth and Derived Views

It is very important not to confuse them.

### Sources of Truth

- `SystemRegistry`
- `TaskRegistry`
- `CapabilityRegistry`
- `PatternRegistry`
- `ReflectionStore`

### Derived Indexes

- `CoverageIndex`
- `SystemLifecycleHealthIndex`
- `PatternUsageIndex`
- `SemanticDriftReport`
- `CapabilityGapView`

Rule:

derived indexes can be recalculated from sources of truth.

This protects against accumulation of garbage and desynchronization.

---

## 2.4 Conflicts Between Layers

Conflicts are inevitable.

Examples:

- Coverage says "a new system is needed";
- Budget says "limit exhausted".

Or:

- Builder proposes a strong expansion;
- SemanticDriftMonitor says "the system is losing identity".

### A Unified Conflict Resolution Model Is Needed

The decision should not be made arbitrarily.

A `GovernanceDecision` is needed.

Example:

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

### Possible Decisions

- `approve`
- `approve_with_constraints`
- `defer`
- `reject`
- `require_human_review`

---

## 2.5 Minimum Coordination Protocol

For a start, this pipeline is sufficient:

1. Registry mutation
2. Event emission
3. Derived index updates
4. Governance checks
5. Final state transition or rollback

### Example

`builder_agent` creates a candidate system:

1. `SystemRegistry.create_candidate`
2. `system_version_created` is published
3. `CoverageIndex` updates coverage graph
4. `BudgetTracker` updates counters
5. `GovernanceAgent` checks conflicts
6. if ok — version remains active candidate
7. if conflict — candidate is moved to `blocked` or `deferred`

---

## 3. Formalizing `DesignTemplate`

The current form of templates is too abstract.

A template is needed not as "analyzer -> executor -> validator", but as a parameterized graph schema.

## 3.1 New Template Format

Example:

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

## 3.2 What It Means to Instantiate a Template

Instantiation is not "the agent figures it out on its own".

It is an operation:

`DesignTemplate + CapabilityBindings + TaskContext -> DraftSystemDefinition`

### `CapabilityBindings`

Example:

```yaml
bindings:
  analyzer:
    bind_to: systems.code_analysis_system
  executor:
    bind_to: agents.code_agent
  validator:
    bind_to: systems.test_validation_system
```

### What the Instantiator Checks

- all required slots are filled;
- bound entity actually implements the capability;
- node type is allowed for the slot;
- edge templates can be materialized;
- interface is consistent with task type.

This is already almost a deterministic construction step.

---

## 3.3 Who Instantiates a Template

A separate component is needed for this:

- `TemplateInstantiator`

It does not "create", but does structural work:

- takes a template;
- checks bindings;
- builds a draft definition;
- returns a list of unresolved constraints.

### If Something Is Missing

`TemplateInstantiator` should not silently invent things.

It should return:

- `missing_capability_binding`
- `invalid_slot_binding`
- `interface_conflict`

Then the builder-agent decides how to close the gap.

---

## 4. Pattern Extraction Pipeline in a Realistic Form

The naive text "find recurring decision sequences" is indeed too optimistic.

A more practical pipeline is needed.

## 4.1 What Data Is Actually Needed

Not only the outcome but also decision traces need to be stored.

New entity:

- `DecisionTrace`

Example:

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

Without `DecisionTrace`, pattern extraction has almost nothing to feed on.

---

## 4.2 How to Cluster Cases

Clusters should not be built by a single criterion.

At minimum, use:

- `task_type`
- `selected_pattern`
- `selected_template`
- `outcome_quality_bucket`
- `human_feedback_bucket`

### Outcome Buckets

- `high_success`
- `mixed_success`
- `failure`

### Human Feedback Buckets

- `positive`
- `neutral`
- `negative`
- `missing`

That is, candidate patterns are extracted not from "all good cases in general", but from structurally similar solutions.

---

## 4.3 Who Validates a Pattern Draft

An explicit lifecycle pattern promotion is needed.

### Roles

- `pattern_extraction_agent` creates a draft;
- `governance_agent` checks consistency;
- `human_reviewer` approves trusted promotion.

### Pattern Statuses

- `draft`
- `candidate`
- `trusted`
- `deprecated`
- `rejected`

### Rule

No automatically extracted pattern becomes `trusted` without review.

This is especially important because trusted patterns will influence future system design.

---

## 5. Unified Roadmap

Now we need to merge the infrastructure roadmap and the meta-cognitive roadmap into one.

## Phase 1. System Foundation

Dependencies:

- schemas for systems;
- `SystemRegistry`;
- `SystemVersion`;
- `SystemRelease`;
- basic invoke via proxy mode.

Result:

- the platform can store and invoke versioned systems.

## Phase 2. Controlled Evolution

Dependencies:

- candidate lifecycle;
- evaluation;
- permissions;
- budgets;
- release channels;
- basic governance.

Result:

- the platform can safely improve and publish systems.

## Phase 3. Knowledge Foundation

Dependencies:

- `TaskRegistry`;
- `CapabilityRegistry`;
- `SemanticContract`;
- linking systems to capabilities and task types.

Result:

- the platform understands what tasks and capabilities it has.

## Phase 4. Pattern Foundation

Dependencies:

- `PatternRegistry`;
- `StrategyPattern`;
- `DesignTemplate`;
- `AntiPattern`;
- `TemplateInstantiator`.

Result:

- the platform gains reusable cognitive structures.

## Phase 5. Reflection and Traceability

Dependencies:

- `ReflectionStore`;
- `DecisionTrace`;
- `reflection_agent`;
- version/pattern reflection flows.

Result:

- the platform starts learning from the process, not just the outcome.

## Phase 6. Coverage and Gap Analysis

Dependencies:

- `CoverageIndex`;
- capability-to-task mapping;
- gap detection;
- expansion proposal flow.

Result:

- self-expansion is triggered by detected coverage gaps.

## Phase 7. Pattern Extraction and Composition

Dependencies:

- `pattern_extraction_agent`;
- `CapabilityCompositionHypothesis`;
- clustering of successful cases;
- governance flow for pattern drafts.

Result:

- the platform starts extracting and combining patterns.

## Phase 8. Drift and Consolidation

Dependencies:

- `SemanticDriftMonitor`;
- `SystemLifecycleHealth`;
- `ConsolidationManager`.

Result:

- the platform can not only grow, but also maintain semantic integrity.

---

## 5.1 Dependency Matrix

In short:

- cannot do `CoverageIndex` before `SystemRegistry` and `CapabilityRegistry`;
- cannot do `PatternExtraction` before `ReflectionStore` and `DecisionTrace`;
- cannot do `TemplateInstantiator` before `DesignTemplate` and capability bindings;
- cannot do full trusted pattern flow before governance + human review hooks.

---

## 6. What Is MVP

If doing it realistically and without overload, the MVP should be:

1. `SystemRegistry` + versioned systems
2. proxy invocation
3. candidate lifecycle + permissions
4. `TaskType`, `Capability`, `SemanticContract`
5. `StrategyPattern` + `DesignTemplate`
6. `TemplateInstantiator`
7. `ReflectionRecord`

This already provides:

- safe systems;
- primary cognitive patterns;
- minimal reflection;
- foundation for future self-expansion.

Automatic pattern extraction and full consolidation can be done later.

---

## 7. Summary

The remaining open questions indeed lie in implementation design, not in basic architecture.

To close them correctly, we need:

- a role model for agents;
- an event-driven coordination protocol;
- instantiable templates;
- a realistic pattern extraction pipeline;
- a unified dependency-aware roadmap.

Only then can the platform be not just described as self-organizing, but actually built in stages.
