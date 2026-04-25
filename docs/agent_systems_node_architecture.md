# Architecture of Agent Systems as Executable Nodes

## Why This Is Needed

Grid already has strong foundations:

- config-oriented description of agents and tools;
- dynamic agents via `AgentFactory`;
- system tools for introspection;
- improvement loop with registry, experiment, evaluation and promotion;
- pipeline/runtime layer for serialized execution.

But the ontology is still fragmented:

- `agents` exist as a primary entity;
- `agent systems` effectively exist but are not formalized as a model object;
- self-improvement lives alongside the runtime, not inside the common architecture;
- system version, experiment and release are not first-class entities.

Because of this, self-development looks like an add-on over config rather than a normal part of the platform.

## Core Idea

We need to raise the abstraction level:

- not `agent` as the only executable entity;
- but `node` as a common executable object;
- where `agent system config` compiles into `system node`.

Then:

- an agent remains a special case of a node;
- an entire agent system also becomes a node;
- system agents can see not only individual agents but entire systems;
- system versions, experiments and test runs become a natural part of the runtime.

## Basic Model

### 1. Node

A unified execution entity.

Node types:

- `agent` — one agent with model, tools and prompt;
- `system` — composition of several nodes with an entry point;
- `tool` — external or built-in function;
- `workflow` — declarative graph of steps;
- `router` — selection of the next node;
- `evaluator` — result checking;
- `benchmark` — evaluation scenario;
- `policy` — access, limits and promotion rules.

### 2. System Definition

Description of an agent system as a graph.

Mandatory properties:

- `id`;
- `version`;
- `entrypoint`;
- `default_agent`;
- `nodes`;
- `interfaces`;
- `policies`;
- `dependencies`.

Rationale:

- `default_agent` is needed for backward compatibility and simple launch;
- `entrypoint` is needed for a correct execution model;
- `nodes` define the internal graph;
- `interfaces` define how the system is called from outside;
- `dependencies` allow the system to use other systems.

### 3. System Version

A system version must be immutable.

This is a key principle; otherwise self-improvement turns into overwriting a live config.

The correct model:

- definition immutable;
- release pointers mutable.

That is, the existing version does not change, only the pointer:

- `stable`;
- `candidate`;
- `experiment`;
- `canary`;
- `archived`.

### 4. System Registry

A separate registry of systems is needed, not just an improvement registry.

It stores:

- list of systems;
- their versions;
- active aliases/channels;
- dependencies between systems;
- interface contracts;
- publication history;
- links to experiments and benchmark results.

## Target Architecture

### Layer 1. Declarative Layer

Config describes not only agents and tools, but also systems.

Something like:

```yaml
systems:
  coding_assistant:
    title: Coding Assistant
    description: System for engineering tasks
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

### Layer 2. Compilation Layer

Config should not be executed directly as YAML at every step.

A compiler is needed:

- `config -> normalized IR -> executable graph`.

The IR must be unified for both a single agent and an entire system.

This eliminates workarounds because the runtime works with one graph model, not different entities.

### Layer 3. Runtime Layer

The runtime must be able to execute any node using the same contract:

- `invoke(node_ref, input, context, policy) -> result`.

Then:

- an agent calls a system the same way as another node;
- a system calls a nested system the same way;
- a self-improvement agent can run candidate versions as regular callable nodes.

### Layer 4. Registry Layer

We need to separate two registries:

- `SystemRegistry` — what exists;
- `ImprovementRegistry` — what we change and how we verify.

The relationship between them:

- a problem relates to a system or system version;
- an experiment creates a candidate version;
- an evaluator checks the candidate;
- promotion moves the channel alias to the new version.

### Layer 5. Evaluation Layer

What should be evaluated is not just `config_diff`, but an executable system version.

Scenario:

1. there is `system@1.4.0`;
2. an agent creates `system@1.5.0-candidate.3`;
3. benchmark runs against the candidate version;
4. canary goes to alias `canary`;
5. after success `stable -> 1.5.0`.

This is much cleaner than "changed a piece of config and somehow applied it".

## How to Turn an Agent System into a Node

### Rule

Any system must have an external call contract.

Minimum contract:

- `system_id`;
- `version_selector`;
- `input`;
- `execution_mode`;
- `constraints`.

Call example:

```json
{
  "system_id": "coding_assistant",
  "version_selector": "stable",
  "input": {
    "task": "Fix flaky tests around config proposer"
  },
  "execution_mode": "sync"
}
```

Response:

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

Then a system agent can:

- get a list of systems;
- request a system's interface;
- invoke a system;
- invoke a specific version;
- compare multiple versions;
- run a benchmark;
- release a candidate.

## What Entities Are Needed in the Data Model

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

Can either extend the current `ImprovementExperiment` or create a related layer on top of it.

The main thing is that the experiment should reference not an abstract diff, but:

- `target_system_id`
- `base_version`
- `candidate_version`
- `change_set`
- `evaluation_suite`

## Proposed Config and File Structure

If done cleanly, it's better to move away from one huge `config.yaml` to directories.

Example:

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

Why this is better:

- systems are separated from agents;
- system version is stored explicitly;
- easier to diff and compare;
- simpler to make candidate and canary;
- multiple parallel variants can be stored without polluting the main file.

## What a System Agent Should Be Able to Do

Not direct access to live config, but a bounded capability surface.

System-level tools are needed:

- `system_list_systems`
- `system_get_system_info`
- `system_get_system_versions`
- `system_invoke_system`
- `system_compare_versions`
- `system_create_candidate_version`
- `system_run_benchmark`
- `system_promote_version`
- `system_archive_version`

Important:

an agent should not "edit config as text".

It should work through domain operations:

- create a version;
- change a node;
- change a dependency;
- switch entrypoint;
- change policy;
- run evaluation.

This is the main anti-workaround principle.

## How to Embed Self-Improvement Without Dead Code

### Bad Path

- separate self-improvement mode;
- separate special-case functions;
- direct editing of the current `config.yaml`;
- promotion as a side effect.

### Good Path

Self-improvement is a regular consumer of the system node platform.

That is, the improver works like this:

1. observes a problem;
2. selects a target system;
3. creates a candidate version;
4. changes the graph definition through domain API;
5. runs a benchmark;
6. runs a canary;
7. moves the release channel.

This is not separate "magic", but a normal lifecycle of any system.

## Interaction with Current Grid Code

Based on the current repo, this breaks down as follows.

### Already Reusable

- `core/agent_factory.py` as runtime for agent nodes;
- `tools/system_tools.py` as basis for introspection;
- `tools/orchestrator_tools.py` as mechanism for executing dynamic graphs;
- `core/pipeline_registry.py` as execution coordination;
- `core/improvement_registry.py` as basis for experiment lifecycle;
- `core/evaluator.py` and `benchmarks/run.py` as evaluation layer.

### What's Missing

- `SystemRegistry`;
- `SystemDefinition` / `SystemVersion` schemas;
- compiler `config -> graph IR`;
- system-level tool surface;
- release/channel management;
- version-aware invocation.

## How This Should Look Structurally in Code

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

## Correct Lifecycle

### 1. Authoring

The system is described declaratively.

### 2. Compile

Config is compiled into a normalized graph IR.

### 3. Register

A version is registered as an immutable artifact.

### 4. Invoke

The system is called by contract through the runtime.

### 5. Observe

Logs, metrics and results are tied to the system version.

### 6. Experiment

A candidate is created based on the version.

### 7. Evaluate

Benchmark/canary are run.

### 8. Promote

The channel alias changes, not the content of the old version.

## Key Principles to Avoid Workarounds

1. One executable abstraction: everything is `node`.
2. A system is not a special mode, but a regular callable node.
3. Versions immutable, channels mutable.
4. Changes only through domain operations, not by editing YAML as text.
5. Evaluation works on system versions, not on "change ideas".
6. Runtime knows nothing about self-improvement as an exception.
7. Introspection and management go through tool/API surface, not through direct access to internals.

## Recommended Implementation Sequence

### Stage 1

Add `systems:` as a new config section and create `SystemRegistry` without changing runtime.

### Stage 2

Create `system_invoke_system`, where system is still proxied to `default_agent`.

This provides compatibility and quick start.

### Stage 3

Add graph IR and `system_runtime` so the system becomes a full node composition.

### Stage 4

Convert improvement loop from `config_diff` to `candidate system version`.

### Stage 5

Extract version/channel/release into a separate layer and connect canary/promotion.

## Short Conclusion

The correct goal is not for "the agent to edit config and improve itself".

The correct goal is for:

- config to describe executable systems;
- a system to be a node;
- a node to have a version and a contract;
- agents to be able to call and compare systems;
- the improvement loop to manage system versions and releases.

Then self-development will not be magical and fragile, but a natural property of the platform.
