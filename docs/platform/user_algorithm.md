# User Algorithm

This document explains the platform MVP as a practical user workflow.

## What the platform is

The platform manages **versioned agent systems**.

A `system` is a named executable unit with:

- `system_id`
- `version`
- `entrypoint`
- `interface`
- `nodes`
- `policy`

In the current MVP, the most common system is a simple proxy system with one
`agent_node` as the entrypoint.

## What the user does

The user does not edit registry files manually.

The intended workflow is:

1. Define a system.
2. Register it as a version.
3. Move it through lifecycle states.
4. Invoke it by version or by channel.
5. Create the next version by clone or mutation.

## Main concepts

### 1. System definition

This is the source artifact the platform understands.

Minimal shape:

```yaml
system_id: demo_platform_system
version: 0.1.0
entrypoint: main
interface:
  input_schema: task_v1
  output_schema: result_v1
nodes:
  main:
    type: agent_node
    agent_ref: agents.chat_agent
policy:
  execution_mode: proxy
```

### 2. Version

Each change produces a new immutable version:

- `0.1.0`
- `0.2.0`
- `0.3.0`

The platform does not mutate old versions in place.

### 3. Channel

A channel is a movable pointer to a version:

- `stable`
- `canary`
- `candidate`

Example:

- `stable -> 0.1.0`
- `canary -> 0.2.0`

### 4. Runtime invocation

The user can invoke:

- a specific version;
- a channel;
- a nested system through `system_ref_node`.

## Clear user algorithm

### Scenario A. Create and run a system

1. Create a `SystemDefinition`.
2. Register it as a new version.
3. Promote it to `stable` if it is approved.
4. Invoke it with input payload.
5. Read `SystemRunResult`.

Expected result:

- the system appears in registry;
- `stable` points to the selected version;
- runtime returns `status`, `final_output`, `node_results`, `warnings`.

### Scenario B. Improve an existing system

1. Take the current stable version.
2. Clone it into a new version.
3. Apply bounded mutations.
4. Register the mutated version as `candidate`.
5. Send it to `canary`.
6. Promote to `stable` or reject it.

Expected result:

- old stable remains intact;
- new version is evaluated separately;
- rollback is possible by moving `stable` back.

### Scenario C. Call a system from another system

1. In the parent system, create a node with type `system_ref_node`.
2. Point it to `target_system` and optional `target_version` or `target_channel`.
3. Compile the parent system.
4. Run the parent system.

Expected result:

- nested call is explicit;
- call depth is controlled by policy;
- cycles are rejected by compiler.

## What the platform accepts

### Input to registration

The registry accepts a full `SystemDefinition`.

### Input to mutation

The mutation layer accepts a `MutationSet`, for example:

- `add_node`
- `update_node`
- `connect_nodes`
- `set_entrypoint`
- `add_dependency`

### Input to runtime

The runtime accepts:

- `system_id`
- `version` or `channel`
- `input_payload`
- `actor_role`

## What the platform returns

### Registration result

The registry returns `SystemVersionRecord`.

Important fields:

- `system_id`
- `version`
- `status`
- `parent_version`
- `created_at`

### Promotion result

Promotion returns `SystemReleaseState`.

Important fields:

- `channels`
- `revision`
- `updated_at`

### Runtime result

Runtime returns `SystemRunResult`.

Important fields:

- `status`
- `final_output`
- `node_results`
- `failed_nodes`
- `warnings`
- `retry_trace`
- `side_effects`

## Practical MVP algorithm for a user

If you are using the current prototype, the practical sequence is:

1. Start from one simple proxy system.
2. Register version `0.1.0`.
3. Mark it `stable`.
4. Invoke it with a small payload.
5. Verify `SystemRunResult`.
6. Clone it into `0.2.0`.
7. Change one thing only.
8. Register `0.2.0` as candidate.
9. Promote or reject it.
10. Repeat.

This is the safest way to use the MVP today.

## What is already real in the MVP

- versioned registry
- stable/canary lifecycle
- compiler validation
- safe predicate AST
- bounded mutation layer
- runtime invocation
- live smoke run with `kimi-k2.5-opencode`

## What is not yet the main user path

These exist only partially or as foundation:

- fully autonomous self-expansion
- real sandbox backend
- full event-driven coordination
- automatic pattern extraction
- advanced multi-node production workflows

## Simple mental model

The easiest mental model is:

- a `system` is a versioned app;
- the `registry` is the package registry;
- a `channel` is a release pointer;
- the `runtime` executes the selected release;
- the `mutation layer` creates the next release safely.

If you follow that model, the current MVP is already understandable and usable.
