# Design of Self-Improvement and Self-Expansion Through System Nodes

## Goal

We need a design where an agent can:

- improve existing systems;
- create new systems;
- connect them to the common registry;
- call them as regular executable objects;
- safely test versions before publication;
- not break the runtime and not proliferate special-case logic.

The key requirement: everything should be logical, simple and uniform.

Therefore, the main idea is:

- everything executable in the platform is represented as `node`;
- everything compositional is represented as `system`;
- all changes happen through creating a new `version`;
- all connections go through `registry` and `release channels`;
- self-improvement and self-expansion use the same lifecycle.

---

## 1. Unified Mental Model

### Main Entities

#### `Node`

Minimal executable unit.

Types:

- `agent`
- `tool`
- `router`
- `workflow`
- `system`
- `evaluator`

#### `System`

Composition of nodes with:

- entry point;
- interface;
- execution graph;
- dependencies;
- policy;
- versions.

#### `Version`

Immutable snapshot of a system definition.

#### `Release Channel`

Pointer to an active version:

- `stable`
- `candidate`
- `canary`
- `dev`
- `archived`

#### `Registry`

A registry that answers the questions:

- what systems exist;
- what versions they have;
- which version is active in which channel;
- who depends on whom;
- what interfaces are exported.

---

## 2. One Lifecycle for Improvement and Expansion

Self-improvement and self-expansion should not be two different subsystems.

They differ only in the base operation:

- self-improvement: create a new version of an existing system;
- self-expansion: create a new system and register it.

The lifecycle is the same afterwards:

1. Initiative
2. Design
3. Build definition
4. Register candidate version
5. Interface verification
6. Benchmark / simulation
7. Canary
8. Promotion
9. Monitoring

This is important: if self-expansion does not go through the same pipeline, it will very quickly turn into chaotic generation of new YAML files.

---

## 3. Three Levels of Changes

To make the system logical, we need to explicitly separate the types of changes.

### Level A. `Tune`

Small changes within an existing system:

- change a node's model;
- change a prompt;
- change the set of tools;
- change a routing rule;
- change a policy;
- change default agent.

This is self-improvement.

### Level B. `Extend`

Adding new internal capabilities:

- a new node in an existing system;
- a new subworkflow;
- a new evaluator;
- a new routing branch.

This is partially self-expansion, but within an existing system.

### Level C. `Create`

Creating a new system:

- new `system_id`;
- new interface;
- new graph;
- new lifecycle;
- publication in registry.

This is full self-expansion.

All three levels should use the same domain operations.

---

## 4. Architectural Principle of Simplicity

The most dangerous mistake here: giving an agent access to arbitrary config editing.

The correct design:

- the agent does not edit the platform structure directly;
- the agent calls domain operations;
- domain operations build a valid definition;
- the runtime executes only registered versions.

That is, instead of:

- "open YAML and write something"

it should be:

- `create_system(...)`
- `fork_system_version(...)`
- `add_node(...)`
- `connect_nodes(...)`
- `set_entrypoint(...)`
- `publish_candidate(...)`

This is the main sign of a non-hacky architecture.

---

## 5. Target Data Structure

## 5.1 `SystemDefinition`

```yaml
id: coding_assistant
title: Coding Assistant
description: System for engineering tasks
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

### What's Important

- `ref` points to already existing entities;
- `nodes` and `edges` define a graph;
- `interfaces` make the system callable from outside;
- `exports.capabilities` provide discovery mechanisms;
- `dependencies` allow building a network of systems.

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

A short index is needed for introspection and discovery.

```yaml
system_id: coding_assistant
title: Coding Assistant
description: System for engineering tasks
latest_stable: 1.2.0
capabilities: [code, planning, refactor]
entrypoint: main
input_schema: coding_task_v1
output_schema: coding_result_v1
discoverable: true
invokable: true
```

---

## 6. How an Agent Should Create a New System

Self-expansion should be a first-class operation.

### Pipeline for Creating a New System

1. Agent identifies a need
2. Agent checks if a similar system already exists
3. Agent designs the interface
4. Agent designs the graph
5. Agent assembles the definition
6. System is validated
7. Version `0.1.0-candidate` is created
8. Smoke tests are run
9. System is registered as discoverable
10. After evaluation, published to `stable`

### What Exactly the Agent Should Decide Before Creation

- what is the goal of the new system;
- what is its input contract;
- what is the expected output;
- what capabilities does it export;
- is it a leaf-system or orchestration system;
- can it call other systems;
- what policies does it need.

### What Should Not Be Allowed

An agent should not:

- immediately write to `stable`;
- overwrite others' stable versions;
- replace a channel without evaluation;
- create a system without an interface and capabilities;
- create circular dependencies without explicit permission.

---

## 7. Self-Expansion as Network Expansion of the Platform

A new system is useful only if it can be discovered and called.

Therefore, after creation, two things must happen:

### 1. `Registration`

The system enters `SystemRegistry` and becomes visible through discovery tools.

### 2. `Capability Export`

The system declares:

- what it can do;
- what its call contract is;
- what execution constraints exist.

Then other agents and systems can find it by capability, not just by name.

Example:

- agent searches `capability=browser_automation`;
- registry returns `web_operator_system`;
- agent calls it by contract.

This is much better than hardcoding knowledge about new systems in prompts.

---

## 8. Discovery and Invocation

To keep everything simple, a standard external API is needed.

## 8.1 Discovery API

Tools:

- `system_list_systems`
- `system_search_systems`
- `system_get_system_info`
- `system_get_system_versions`
- `system_get_system_dependencies`

## 8.2 Invocation API

Tools:

- `system_invoke`
- `system_invoke_version`
- `system_invoke_capability`

### Example Call by ID

```json
{
  "system_id": "coding_assistant",
  "channel": "stable",
  "input": {
    "task": "Find the cause of flaky test"
  }
}
```

### Example Call by Capability

```json
{
  "capability": "document_analysis",
  "channel": "stable",
  "input": {
    "file_path": "docs/spec.pdf",
    "goal": "extract requirements"
  }
}
```

### What the Runtime Does

1. resolves the system;
2. resolves the channel to a version;
3. loads the definition;
4. compiles to graph IR;
5. executes the entrypoint;
6. returns a structured result.

---

## 9. Two Execution Modes for a System

To avoid complicating the implementation, the system should support two modes.

### Mode 1. `Proxy Mode`

For the early stage.

The system simply proxies to `default_agent`.

Pros:

- easy to implement;
- backward compatible;
- can quickly connect `systems:` without rewriting runtime.

### Mode 2. `Graph Mode`

Full runtime.

The system executes as a graph of nodes.

Pros:

- real composition;
- self-expansion can build internal structures;
- evaluator sees real system behavior;
- dependency management becomes clean.

The right path: first Proxy Mode, then Graph Mode.

---

## 10. Self-Improvement Design

Self-improvement should rely not on "file diff", but on "definition diff".

### System Improvement Looks Like This

1. select `target_system`;
2. select `base_version`;
3. create a fork candidate version;
4. apply change-set to definition;
5. run validation;
6. run benchmark;
7. record results;
8. switch candidate/stable channel on success.

### Types of Improvements

- replace a node's model;
- rewrite a node's prompt;
- change edge conditions;
- add an evaluator;
- narrow or expand tools;
- change default entry behavior.

### Change-set Should Be Stored as Domain Mutations

Not like this:

```json
{"path":"agents.chat_agent.model","new":"x"}
```

But like this:

```json
{
  "op": "replace_node_agent_model",
  "node_id": "main",
  "new_model": "gpt-5.4-mini"
}
```

Or:

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

This is much more resilient to future structural changes.

---

## 11. Self-Expansion Design

Self-expansion should be a constrained constructor of new systems.

### Basic Workflow

1. Agent discovers an unmet capability
2. Checks existing systems
3. If no suitable system exists:
   - designs interface;
   - designs graph;
   - proposes definition;
   - registers candidate;
   - runs validation;
   - runs smoke tests;
   - publishes to `candidate` channel;
   - after evaluation, promotes to `stable`.

### Constraints

- max systems per day (budget);
- mandatory interface and capability declaration;
- no self-approval for `stable`;
- no circular dependencies;
- observable lifecycle.

---

## 12. Rules for Self-Expansion

### 12.1 Interface First

A system without an interface cannot be registered.

### 12.2 Capability Declaration

Every system must declare its capabilities.

### 12.3 Discoverability

A system must be discoverable by capability and interface contract.

### 12.4 Dependency Hygiene

A system cannot have unregistered dependencies.

### 12.5 Traceability

Every version must be traceable to its source.

### 12.6 Promotion Gate

Promotion to `stable` requires evaluation.

### 12.7 No Silent Overwrites

Channel aliases are changed explicitly.

### 12.8 Audit Log

Mutations are logged.

---

## 13. System-Level Tool Surface

For agents to manage systems, tools are needed.

### `system_list_systems`

- Returns all registered systems with their status.

### `system_get_system_info`

- Returns full system definition for a given ID.

### `system_get_system_versions`

- Returns version history of a system.

### `system_invoke_system`

- Invokes a system by ID+channel.

### `system_create_candidate_version`

- Creates a new version from a definition change-set.

### `system_run_benchmark`

- Runs benchmark on a specific version.

### `system_promote_version`

- Moves a channel alias to a new version.

### `system_archive_version`

- Marks a version as archived.

---

## 14. Environment and Security

### Sandbox

Candidate systems run in a sandbox:

- no access to `stable` data;
- no access to production channels;
- limited resources;
- isolated execution.

### Permissions

Not all agents can create systems.

### Audit

All mutations are logged:

- who created;
- when;
- what changed;
- what was the result.

---

## 15. Evaluation and Quality Gates

### Minimum Quality Gates

1. **Definition validation**: syntax, schema, completeness.
2. **Interface compatibility**: contract compliance.
3. **Smoke tests**: basic operability.
4. **Benchmark**: regression measurement.
5. **Canary**: limited rollout.
6. **Manual gate** (optional): for high-risk changes.

### Quality Metrics

- benchmark score;
- regression delta;
- success rate;
- error rate;
- response time.

---

## 16. Version Naming and Channel Semantics

### Version Format

`MAJOR.MINOR.PATCH[-channel]`

Examples:
- `1.0.0`
- `1.1.0-candidate.3`
- `1.2.0-canary`

### Channel Semantics

- `stable` — production, only from evaluated versions;
- `candidate` — ready for evaluation;
- `canary` — limited real traffic;
- `dev` — development, unstable;
- `archived` — not in use.

---

## 17. System Composition Rules

### Allowed Compositions

- `agent_node` — regular agent node;
- `system_ref_node` — reference to another system (nesting);
- `router_node` — decision node;
- `workflow_node` — structured workflow;
- `evaluator_node` — evaluation node;

### Prohibited

- circular composition;
- self-reference without explicit permission;
- untyped nodes.

---

## 18. Registry and Indexing

`SystemRegistry` stores:

- list of systems;
- version tree;
- release channels;
- dependencies;
- interface schemas;
- capability index.

### Indexes

- `systems_by_capability`;
- `systems_by_task_type`;
- `dependency_graph`;
- `channel_versions`.

---

## 19. Directory Structure

For working with systems, a clear file structure is needed.

```text
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

### Why This Is Convenient

- an agent can easily create a new system as a new directory;
- a version is a separate file;
- release channels are not mixed with definition;
- comparison and audit are simple;
- rollback is trivial.

---

## 20. Minimal Classes for Implementation

### In `schemas/`

- `SystemDefinition`
- `SystemNode`
- `SystemEdge`
- `SystemInterface`
- `SystemVersion`
- `SystemReleaseState`
- `SystemMutation`
- `SystemRunResult`

### In `core/`

- `system_registry.py`
- `system_compiler.py`
- `system_runtime.py`
- `system_mutator.py`
- `system_release_manager.py`
- `system_discovery.py`

### In `tools/`

- `system_registry_tools.py`
- `system_design_tools.py`
- `system_runtime_tools.py`
- `system_release_tools.py`

---

## 21. Phased Implementation Without Overload

## Stage 1. Registry-first

Create:

- `SystemDefinition`
- `SystemVersion`
- `SystemRegistry`
- `system_list_systems`
- `system_get_system_info`

No complex runtime yet.

## Stage 2. Proxy invocation

Create:

- `system_invoke`
- `system_create_draft`
- `system_register_candidate`

System is invoked through `default_agent`.

## Stage 3. Mutation API

Create:

- operations for changing system definition;
- candidate version lifecycle;
- version-aware validation.

## Stage 4. Graph runtime

Create:

- compiler;
- graph IR;
- execution engine for system nodes.

## Stage 5. Improvement migration

Migrate current improvement loop to system versions.

## Stage 6. Self-expansion

Allow agents to:

- create new systems;
- publish candidate;
- connect dependencies;
- use capability discovery.

---

## 22. The Most Important Simplification

If you need to keep everything simple, remember one rule:

### The agent does not manage the config.

### The agent manages the system registry and system versions through domain operations.

This is the central principle.

As long as it is followed:

- self-improvement remains controlled;
- self-expansion remains meaningful;
- the architecture does not turn into a bunch of YAML-hacking agents;
- new systems become part of the platform, not project clutter.

---

## 23. Final Formula

The correct platform should be thought of as:

- `agent` solves problems;
- `system` composes capabilities;
- `registry` makes systems discoverable;
- `version` makes changes safe;
- `channels` make releases manageable;
- `evaluation` makes growth verifiable;
- `mutation API` makes self-change structural;
- `self-expansion` is creating new systems through the same lifecycle.

Then an agent can truly:

- conceive a new system;
- design an interface;
- assemble a graph;
- register a candidate;
- test it;
- connect it as a dependency;
- start using it;

and all of this will not be a hack, but a natural behavior of the platform.

---

## 24. Architectural Corrections

This section clarifies and tightens the design where ambiguities were present in the base version of the document.

## 24.1 Terminology and Recursion

To eliminate the loop between `system` as a node type and `System` as a top-level entity, three different terms are introduced.

### `ExecutableNode`

Types of executable runtime nodes:

- `agent_node`
- `tool_node`
- `router_node`
- `workflow_node`
- `system_ref_node`
- `evaluator_node`

### `SystemDefinition`

This is not a node type, but a versioned graph artifact.

`SystemDefinition` has:

- `entrypoint`
- `interface`
- `graph`
- `policy`
- `version`

### `SystemRefNode`

The only allowed way to nest a system within a system.

Example:

```yaml
nodes:
  docs_worker:
    type: system_ref_node
    target_system: document_analysis
    target_channel: stable
```

### Depth Limit

Recursion is only allowed as nested invocation via `system_ref_node`.

There must be an explicit limit:

```yaml
runtime_limits:
  max_system_call_depth: 3
```

Checks:

- compiler rejects statically detectable cycles;
- runtime rejects exceeding `max_system_call_depth`.

## 24.2 Edge Conditions Without eval

Free-form string expressions for `when:` are prohibited.

Bad:

```yaml
when: input.task_complexity == "high"
```

Correct:

```yaml
when:
  op: eq
  left:
    var: input.task_complexity
  right:
    value: high
```

Or:

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

### Who Evaluates

`ConditionEvaluator`, not `eval`.

`ConditionEvaluator`:

- only understands allowlisted operations;
- works only with a typed runtime context;
- cannot execute code;
- has no access to imports, FS, network and Python objects.

Allowed operations:

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

Allowed value sources:

- `input.*`
- `state.*`
- `node_output.<node_id>.*`
- `context.flags.*`

Enforcement:

- compiler validates predicate shape and operator applicability;
- runtime validates types and data availability.

## 24.3 Rules Must Be Enforceable

The rules from section 12 are not wishes. They must be enforced by three layers.

### `Compiler Enforcement`

Checks:

- interface-first;
- dependency hygiene;
- schema compatibility;
- graph correctness;
- `system_ref_node` correctness;
- predicate validity.

### `Registry Enforcement`

Checks:

- lifecycle transitions;
- version uniqueness;
- channel move preconditions;
- budget limits;
- ownership and permissions;
- required evaluation artifacts.

### `Runtime Enforcement`

Checks:

- max depth;
- invocation permissions;
- sandbox profile;
- resource ceilings;
- failure policy;
- side-effect policy.

Result:

- every architectural rule must be represented as a compile-time, registry-time or runtime invariant.

## 24.4 Budget as a Subsystem

Budget must be formalized as a policy and stored in a control state store.

Example:

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

### Counters

Minimum needed:

- `new_systems_created_today`
- `candidate_versions_open[system_id]`
- `active_canaries`
- `promotions_today`

### Where Stored

Not in runtime memory, but in registry backend or associated control store.

### Who Updates

- `create_system`
- `register_candidate`
- `start_canary`
- `promote_channel`
- `archive_candidate`
- `reject_candidate`

### Who Resets

- windowed scheduler;
- or rolling-window evaluator.

### Behavior on Exceed

- `hard_fail` in production;
- `soft_warning` allowed only in dev/test.

All budget checks must be performed atomically within a registry transaction.

## 24.5 Lifecycle of Early Implementation Stages

At Stages 1-2, the final production lifecycle is not yet available.

Therefore, the lifecycle must be described as staged.

### Stage A. Registry-first

Has:

- draft;
- schema validation;
- registry entry;
- manual review.

Does not have:

- canary;
- graph runtime;
- full benchmark orchestration.

### Stage B. Proxy invocation

Has:

- invocation via `default_agent`;
- smoke tests;
- candidate channel;
- manual promotion gate.

Does not have:

- node-level failure semantics;
- nested system runtime;
- full release automation.

### Stage C. Full graph runtime

Has:

- graph execution;
- canary;
- failure policies;
- budget enforcement;
- release channel controls.

Consequently, the lifecycle from section 2 is the target production model, not a literal model for Stage A-B.

## 24.6 Benchmark Governance and Ground Truth

If an agent itself builds a system and itself writes the benchmark, this is insufficient for stable promotion.

Provenance of benchmark suites must be explicitly distinguished.

### Suite Sources

- `human_curated`
- `production_trace_curated`
- `agent_proposed_pending_review`
- `synthetic_low_confidence`

### Trust Levels

- `gold`
- `silver`
- `bronze`

### Promotion Rule

- `stable` promotion relies only on `gold` and `silver`;
- `bronze` is allowed only for early candidate screening;
- cold-start systems cannot auto-promote to `stable` based only on synthetic benchmarks.

### Who Curates Benchmarks

- a human;
- or a trusted governance process, separated from the builder-agent.

## 24.7 Concurrency and Write Contention

A multi-agent environment requires an explicit model of concurrent mutations.

### Conflicts

- two candidates for one system;
- simultaneous writes to release channels;
- parallel promotions;
- writes to system index;
- budget counter updates.

### Minimum Requirements

- transactional registry backend;
- revision number on system state;
- per-system lock;
- compare-and-swap for channel promotion.

### Promotion

Promotion must work like this:

1. read current channel version;
2. attempt to update only if the version is still the expected one;
3. on mismatch, return `channel_conflict`.

### Note on Backend

For write-heavy scenarios, JSON files are a poor foundation.

If JSON remains at the start:

- a lock manager or single writer process is needed.

Target option:

- SQLite or another transactional store.

## 24.8 Security Model

Self-expansion without a security model is unacceptable.

### Subjects

- `human_admin`
- `human_reviewer`
- `system_agent`
- `builder_agent`
- `runtime_agent`
- `observer_agent`

### Actions

- `create_system`
- `create_candidate`
- `modify_definition`
- `run_candidate`
- `promote_candidate`
- `move_stable_channel`
- `invoke_system`
- `add_dependency`

### Rights Model

Capability-based access control.

Example:

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

### Basic Restrictions

- not every agent can create systems;
- not every agent can move `stable`;
- candidate systems run only in sandbox profile;
- stable system does not depend on untrusted candidate without explicit policy exception;
- cross-system invocation passes permission check.

## 24.9 Failure Propagation and Rollback

In Graph Mode, failure semantics must be part of the model.

Each node must have a `failure_policy`.

Example:

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

Allowed modes:

- `fail_run`
- `continue_with_warning`
- `skip_node`
- `fallback_to_node`

`SystemRunResult` must contain:

- `status`
- `final_output`
- `node_results[]`
- `failed_nodes[]`
- `warnings[]`
- `retry_trace[]`
- `side_effects[]`

### What Is Rollback

We need to distinguish:

- `release rollback`
  Channel shift to a previous version.
- `state rollback`
  Rollback only of reversible side effects.
- `no rollback possible`
  For irreversible external effects.

Therefore, each side effect must be labeled:

- `reversible`
- `irreversible`
- `compensation_handler`

Without this, rollback cannot be called trivial.

## 24.10 Cold-Start Mode for Self-Expansion

Structurally, self-improvement and self-expansion use a similar lifecycle, but their epistemic situation is different.

A separate mode is needed for new systems:

### `cold_start_expansion`

Steps:

1. `proposal`
2. `interface review`
3. `definition validation`
4. `smoke benchmark`
5. `sandbox trial`
6. `limited discoverability`
7. `human or trusted-policy review`
8. `stable publication`

Special features:

- no baseline;
- no regression history;
- no production confidence;
- higher requirements for interface review and sandbox trial;
- auto-promotion to `stable` is prohibited by default.

Summary:

- one lifecycle framework;
- different validation regime.
