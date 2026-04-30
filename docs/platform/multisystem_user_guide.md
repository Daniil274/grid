# Multi-System User Guide

> **Audience:** Developers and power users of the Grid platform
> **Prerequisites:** [Platform User Algorithm](user_algorithm.md), basic understanding of [SystemDefinition](../agent_systems_node_architecture.md)
> **Status:** Draft — target UX for multi-system features

---

## Table of Contents

1. [What is a Multi-System](#what-is-a-multi-system)
2. [Quick Start](#quick-start)
3. [Defining a System with its Own Config](#defining-a-system-with-its-own-config)
4. [Working with Namespaces](#working-with-namespaces)
5. [Calling Systems Across Namespaces](#calling-systems-across-namespaces)
6. [Cross-System Events](#cross-system-events)
7. [Discovering and Composing Systems](#discovering-and-composing-systems)
8. [Recipes](#recipes)
9. [Troubleshooting](#troubleshooting)
10. [Reference: All System Tools](#reference-all-system-tools)

---

## What is a Multi-System

A **System** in Grid is a versioned graph of executable nodes (agents, tools, other systems) with a defined interface. You create, register, and run systems through the platform.

**Multi-System** means:

- Multiple systems coexist in the platform
- Each system can have its **own agents, tools, and models** — not just the global ones
- Systems are organised into **namespaces** (like deployment environments)
- Systems can **call each other** across namespaces
- Systems can **listen to events** from other systems
- Systems can be **discovered** by what they do (capabilities) and **composed** into pipelines

> **Mental model:** A system is like a microservice. A namespace is like a Kubernetes namespace. The RegistryHub is like a container registry. Channels (`stable`, `canary`) are like deployment slots.

---

## Quick Start

### 1. Create your first isolated system

Ask the system agent to create a system with its own agents:

```
You: Create a system "python-reviewer" that has a reviewer agent
     using claude-sonnet and a fixer agent using deepseek.
     The reviewer checks code, the fixer fixes issues.
```

The system agent will:

1. Generate a `SystemDefinition` with `embedded_config` containing both agents
2. Register it in the `default` namespace
3. Deploy to `canary`
4. Optionally promote to `stable`

### 2. Run the system

```
You: Run python-reviewer on file src/main.py
```

The platform:
1. Loads the system's own `embedded_config` (reviewer + fixer agents)
2. Merges with global config for any missing tools/models
3. Executes the graph: `review → (if issues) fix → report`

### 3. Check what you have

```
You: List all systems in my namespace
```

The agent calls `system_list_systems(namespace="default")` and returns:
- `python-reviewer` v1.0.0 (stable)
- ... any other systems you've created

---

## Defining a System with its Own Config

### Why embed config in a system

By default, `agent_ref` in a system node points to the global `config.yaml`:

```yaml
nodes:
  main:
    type: agent_node
    agent_ref: "agents.chat_agent"   # Must be in config.yaml
```

This is fine for simple systems, but when you want a system to be **self-contained** (portable, isolated, override-specific), you use `embedded_config`.

### `embedded_config` structure

`embedded_config` is the same schema as `config.yaml` — but only the parts you need:

```yaml
embedded_config:
  agents:       # Agents specific to this system
    my_agent:
      name: "My Agent"
      model: "claude-sonnet-4-20250514"
      system_prompt: "..."
      tools: [file_read, file_edit]
      temperature: 0.2

  tools:        # Tools specific to this system
    my_tool:
      type: python
      module: "bundle_tools.helpers"
      function: "do_something"

  models:       # Model overrides
    custom-model:
      provider: openrouter
      model: "anthropic/claude-sonnet-4"
      max_tokens: 16384
```

### Resolution order

When the system runs and needs to resolve `agent_ref: "agents.my_agent"`:

| Priority | Source | Description |
|----------|--------|-------------|
| 1 | `system.embedded_config.agents` | System's own definition |
| 2 | `global_config.agents` | Root `config.yaml` |
| 3 | Error | `AgentNotFoundError` |

This means you can **override** a global agent by giving a system-level agent the same key.

### Full example: self-contained code review system

```yaml
system_id: code_reviewer
version: 1.0.0
description: "Reviews Python code for bugs, style, and security issues"
entrypoint: review

interface:
  inputs:
    code: { type: string, required: true }
    language: { type: string, default: "python" }
  outputs:
    report: { type: string }
    issues_count: { type: integer }
    issues: { type: array }

  capabilities: [code_review, static_analysis]
  tags: [python, security, quality]

embedded_config:
  agents:
    reviewer:
      name: "Code Reviewer"
      model: "claude-sonnet-4-20250514"
      system_prompt: |
        You are a thorough code reviewer. Check for:
        1. Bugs and logic errors
        2. Style violations (PEP 8)
        3. Security vulnerabilities
        4. Performance issues
        Output a structured report with severity levels.
      tools:
        - file_read
        - grep_tool
      temperature: 0.1

    summarizer:
      name: "Report Summarizer"
      model: "glm-4.7-flash"
      system_prompt: "Summarize the review findings into a concise executive summary."
      tools: []
      temperature: 0.3

nodes:
  review:
    type: agent_node
    agent_ref: "agents.reviewer"

  summarize:
    type: agent_node
    agent_ref: "agents.summarizer"

edges:
  - from: review
    to: summarize

policy:
  max_time_seconds: 600
  max_tokens_total: 200000
  allow_tools: [file_read, grep_tool]
```

### When NOT to use `embedded_config`

- When your system only uses global agents — keep `agent_ref` pointing to `config.yaml`
- When you want agent definitions shared across many systems — keep them in `config.yaml`
- For quick prototypes — use `orchestrate()` with ad-hoc agents instead

---

## Working with Namespaces

### Concept

Namespaces separate systems into logical groups. Think of them as:

- **Environments:** `dev`, `staging`, `prod`
- **Tenants:** `team-alpha`, `team-beta`
- **Projects:** `project-1`, `project-2`

Each namespace has its own `SystemRegistry` — systems in `dev` are completely invisible to `prod` unless explicitly exposed.

### Default namespace

If you never specify a namespace, everything goes into `default`. This is backward-compatible with existing behaviour.

### Creating a namespace

```
You: Create namespace "team-alpha"
```

The system agent calls:

```python
system_create_namespace("team-alpha")
# Returns: { namespace: "team-alpha", registry_path: "data/platform/team-alpha/registry.json" }
```

### Listing namespaces

```
You: What namespaces exist?
```

```python
system_list_namespaces()
# Returns: ["default", "team-alpha", "team-beta"]
```

### Creating a system in a specific namespace

```
You: Create a system "ci-runner" in namespace "team-alpha"
```

The system is registered in `team-alpha`'s registry — it won't appear when listing systems in `default`.

### Moving a system between namespaces

```
You: Move code-reviewer from default to team-alpha
```

```python
system_export_system(
    system_id="code-reviewer",
    namespace="default",
    channel="stable"
)
# → returns the full SystemDefinition

system_import_system(
    system_definition=exported_def,
    target_namespace="team-alpha",
    channel="canary"            # Start in canary in new namespace
)
```

---

## Calling Systems Across Namespaces

### From a graph: `SystemRefNode` with namespace

```yaml
nodes:
  call_reviewer:
    type: system_ref_node
    target_system: code-reviewer
    target_namespace: team-alpha      # ← cross-namespace call
    target_channel: stable
```

### From an agent: `system_invoke_system()`

```python
# Agent calls this via a tool:
result = system_invoke_system(
    system_id="code-reviewer",
    namespace="team-alpha",           # ← explicit namespace
    channel="stable",
    inputs={
        "code": "...",
        "language": "python"
    }
)

print(result["outputs"]["report"])
print(result["namespace"])            # "team-alpha"
print(result["version"])             # "1.0.0"
```

### Permissions for cross-namespace calls

The **receiving** system controls who can call it:

```yaml
# In code-reviewer's SystemDefinition:
policy:
  allow_cross_namespace_calls: true
  allowed_callers:
    - namespace: "team-alpha"
      system_ids: ["**"]              # Any system in team-alpha
    - namespace: "ci"
      system_ids: ["ci-runner"]       # Only ci-runner in namespace ci
```

If the caller is not in the allowed list, the call fails with `PermissionDeniedError`.

### Call depth limit

Cross-system calls can chain. The platform enforces a maximum depth:

```
User → System A (depth=0)
  → System B (depth=1)
    → System C (depth=2)
      → System D (depth=3)
        → ERROR: max_system_call_depth exceeded (default: 3)
```

Set per-system:

```yaml
policy:
  max_system_call_depth: 5    # Allow deeper chains for this system
```

---

## Cross-System Events

### Concept

Systems emit events during execution. Other systems can subscribe to these events — even across namespace boundaries.

### Available event types

| Event | Emitted when | Payload |
|-------|-------------|---------|
| `system.invocation.started` | System begins execution | `{system_id, namespace, version, inputs}` |
| `system.invocation.completed` | System finishes successfully | `{system_id, namespace, version, outputs, elapsed_ms}` |
| `system.invocation.failed` | System fails | `{system_id, namespace, version, error}` |
| `system.node.started` | A node begins | `{node_id, node_type, system_id}` |
| `system.node.completed` | A node finishes | `{node_id, outputs, elapsed_ms}` |
| `system.version.promoted` | Version promoted to stable | `{system_id, version, previous_version}` |

### Subscribing from an agent

```python
# Subscribe to code review completions in team-alpha:
system_subscribe_event(
    event_type="system.invocation.completed",
    namespace="team-alpha",             # Only events from team-alpha
    source_system="code-reviewer",      # Only from this system
    handler="my_notification_handler"   # Registered callback
)
```

### Subscribing globally (requires permission)

```python
system_subscribe_event(
    event_type="system.invocation.failed",
    namespace="**",                     # All namespaces
    handler="global_error_handler"
)
```

The subscribing system must have:

```yaml
policy:
  allow_cross_namespace_events: true
  allowed_event_sources:
    - namespace: "**"
      system_ids: ["**"]
```

### Reacting in a graph: `event_trigger` node

```yaml
nodes:
  on_review_complete:
    type: event_trigger_node
    event_type: system.invocation.completed
    source_namespace: team-alpha
    source_system: code-reviewer
    action: notify_slack            # Next node to execute

  notify_slack:
    type: agent_node
    agent_ref: "agents.notifier"
    input_map:
      message: "$event.outputs.report"
```

---

## Discovering and Composing Systems

### Finding systems by what they do

Instead of knowing the exact `system_id`, you can search by capability:

```python
# Find all code review systems:
results = system_search_systems(
    query="code review",
    capabilities=["code_review"],
    namespace=None,                   # Search all namespaces
    channel="stable",
    limit=5
)

# Returns:
# [
#   {
#     system_id: "code-reviewer",
#     namespace: "team-alpha",
#     version: "1.0.0",
#     capabilities: ["code_review", "static_analysis"],
#     tags: ["python", "security"],
#     score: 0.95
#   },
#   {
#     system_id: "pr-analyzer",
#     namespace: "shared",
#     version: "2.1.0",
#     capabilities: ["code_review", "pr_analysis"],
#     tags: ["github", "typescript"],
#     score: 0.82
#   }
# ]
```

### Composing systems into a pipeline

Once you've found the systems you want, compose them on the fly:

```python
# Wire up: review → fix → notify
composed_system = system_compose(
    systems=[
        {"system_id": "code-reviewer", "namespace": "team-alpha", "alias": "review"},
        {"system_id": "auto-fixer",    "namespace": "shared",     "alias": "fix"},
        {"system_id": "slack-notifier","namespace": "team-alpha", "alias": "notify"},
    ],
    edges=[
        {
            "from_alias": "review",
            "to_alias": "fix",
            "input_map": {
                "issues": "issues",       # review's output "issues" → fix's input "issues"
                "code": "code"            # review's output "code" → fix's input "code"
            }
        },
        {
            "from_alias": "fix",
            "to_alias": "notify",
            "input_map": {
                "report": "message"       # fix's output "report" → notify's input "message"
            },
            "condition": "output.fixed == true"   # Only notify if something was fixed
        }
    ]
)

# composed_system is now a SystemDefinition registered in your namespace.
# You can run it immediately:
result = system_invoke_system(
    system_id=composed_system.system_id,
    namespace="default",
    inputs={"code": "...", "language": "python"}
)
```

### Composition that an agent can do conversationally

```
You: Create a pipeline that reviews my code, fixes issues,
     and sends a Slack notification.

Agent: I found these systems:
       - code-reviewer (team-alpha) — reviews Python code
       - auto-fixer (shared) — auto-fixes common issues
       - slack-notifier (team-alpha) — sends Slack messages

       Let me compose them into a pipeline and run it on your code.
```

---

## Recipes

### Recipe 1: Isolated production and development environments

```bash
# Create namespaces
system_create_namespace("prod")
system_create_namespace("dev")

# Deploy code-reviewer v1.0.0 to dev
system_import_system(definition, target_namespace="dev", channel="stable")

# After testing, promote to prod
system_export_system("code-reviewer", namespace="dev")
system_import_system(definition, target_namespace="prod", channel="canary")
system_promote_to_stable("code-reviewer", namespace="prod", version="1.0.0")
```

### Recipe 2: Multi-tenant SaaS with shared base systems

```
Namespaces:
  tenant-a/     ← customer A, has custom "notifier" system
  tenant-b/     ← customer B, has custom "notifier" system
  shared/       ← base systems available to all tenants

Shared systems:
  shared/code-reviewer (stable)
  shared/auto-fixer (stable)

Tenant-specific:
  tenant-a/slack-notifier → posts to customer A's Slack
  tenant-b/email-notifier → emails customer B's team
```

A tenant can compose shared systems with their own:

```python
system_compose(
    systems=[
        {"system_id": "code-reviewer", "namespace": "shared",    "alias": "review"},
        {"system_id": "auto-fixer",    "namespace": "shared",    "alias": "fix"},
        {"system_id": "slack-notifier","namespace": "tenant-a",  "alias": "notify"},
    ],
    edges=[...]
)
```

### Recipe 3: Event-driven CI pipeline

```
Systems:
  code-reviewer → emits "review.completed"
  auto-fixer    → subscribes to "review.completed" (only when issues_count > 0)
                 → emits "fix.completed"
  test-runner   → subscribes to "fix.completed" (runs tests after fix)
                 → emits "tests.completed"
  slack-notifier→ subscribes to "tests.completed"
                 → posts results to Slack
```

All systems are in namespace `ci`. The event chain is:

```
code-reviewer  →  auto-fixer  →  test-runner  →  slack-notifier
   (emit)        (subscribe)     (subscribe)      (subscribe)
```

### Recipe 4: System with custom model provider

Use `embedded_config.models` to give a system its own model:

```yaml
embedded_config:
  models:
    my_specialist:
      provider: openrouter
      model: "qwen/qwen-2.5-coder-32b"
      max_tokens: 32768
      temperature: 0.0

  agents:
    specialist_agent:
      name: "Code Specialist"
      model: "my_specialist"        # ← uses the system's own model
      system_prompt: "..."
      tools: [...]
```

---

## Troubleshooting

### "System not found in namespace"

```
Error: SystemNotFoundError: system_id="code-reviewer", namespace="team-alpha"
```

**Causes:**
- System was created in a different namespace
- System exists but on a different channel (e.g., `canary`, not `stable`)
- Namespace doesn't exist yet

**Fix:**
```python
# Check which namespaces exist:
system_list_namespaces()

# Check what's in the namespace:
system_list_systems(namespace="team-alpha")

# Try a different channel:
system_invoke_system("code-reviewer", namespace="team-alpha", channel="canary", ...)
```

### "Permission denied for cross-namespace call"

```
Error: PermissionDeniedError: caller namespace="team-beta" is not in allowed_callers
```

**Fix:** Update the target system's policy to allow the caller:

```yaml
policy:
  allow_cross_namespace_calls: true
  allowed_callers:
    - namespace: "team-beta"
      system_ids: ["**"]
```

Or use `system_mutate_version()` to update the policy.

### "Max system call depth exceeded"

```
Error: MaxCallDepthExceededError: depth=4 exceeds maximum=3
```

**Causes:**
- Circular dependency between systems
- Too many chained calls

**Fix:**
1. Check for circular `SystemRefNode` references
2. Increase `max_system_call_depth` in the outermost system's policy
3. Restructure to reduce chain depth

### "Agent not found: agents.my_agent"

When using `embedded_config`, an agent reference can't be resolved.

**Checklist:**
- [ ] The agent is defined in `embedded_config.agents` with the exact key used in `agent_ref`
- [ ] The agent key in `embedded_config` doesn't have a typo
- [ ] If the agent should come from global config, remove the `embedded_config.agents` entry (don't define it locally)

### Cross-namespace events not received

**Checklist:**
- [ ] Subscribing system has `allow_cross_namespace_events: true`
- [ ] Subscribing system's `allowed_event_sources` includes the source namespace
- [ ] Event type matches exactly (use `system_list_events()` to see emitted events)
- [ ] Source system is actually emitting events (check source system's policy)

---

## Reference: All System Tools

### Registry & namespace tools

| Tool | Description |
|------|-------------|
| `system_create_namespace(namespace)` | Create a new namespace |
| `system_list_namespaces()` | List all namespaces |
| `system_delete_namespace(namespace)` | Delete a namespace (must be empty) |
| `system_list_systems(namespace="default", channel=None)` | List systems in a namespace |
| `system_get_system(system_id, namespace="default", channel="stable")` | Get full system definition |
| `system_register_version(system_def, namespace="default", channel="candidate")` | Register a new version |
| `system_promote_to_stable(system_id, namespace, version)` | Promote to stable |
| `system_export_system(system_id, namespace, channel)` | Export full definition |
| `system_import_system(system_def, target_namespace, channel)` | Import into namespace |

### Invocation tools

| Tool | Description |
|------|-------------|
| `system_invoke_system(system_id, inputs, *, namespace, channel, version)` | Invoke a system |
| `system_run_graph(system_id, inputs, *, namespace, channel)` | Run full graph mode |
| `system_get_invocation_status(trace_id)` | Get status of an async invocation |

### Discovery & composition tools

| Tool | Description |
|------|-------------|
| `system_search_systems(query, *, capabilities, tags, namespace, channel)` | Search by capability |
| `system_compose(systems, edges, *, namespace)` | Compose systems into a pipeline |
| `system_get_capabilities()` | List all known capabilities across namespaces |

### Event tools

| Tool | Description |
|------|-------------|
| `system_subscribe_event(event_type, *, namespace, source_system, handler)` | Subscribe to events |
| `system_unsubscribe_event(subscription_id)` | Remove a subscription |
| `system_list_subscriptions()` | List current subscriptions |
| `system_list_events(namespace=None, system_id=None, limit=50)` | List recent events |

### Mutation & lifecycle tools

| Tool | Description |
|------|-------------|
| `system_mutate_version(system_id, mutations, *, namespace)` | Edit a system definition |
| `system_clone_version(system_id, new_system_id, *, namespace, target_namespace)` | Clone to new system |
| `system_rollback(system_id, *, namespace)` | Rollback stable to previous version |
| `system_reject_version(system_id, version, *, namespace)` | Reject a candidate |

---

## See Also

- [Multi-System Architecture Design](multisystem_design.md) — architecture and implementation details
- [Platform User Algorithm](user_algorithm.md) — core system lifecycle workflow
- [Agent Systems Node Architecture](../agent_systems_node_architecture.md) — how systems are built from nodes
- [Self-Expansion Design](../agent_systems_self_expansion_design.md) — systems that create and improve systems
- [Cross-Layer Coordination](../cross_layer_coordination_and_agent_roles.md) — agent roles and event coordination
- [Platform Current Status](current_status.md) — what's implemented today
