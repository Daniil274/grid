# Multi-System Architecture Design

> **Status:** Draft — target architecture for Phase 4 (Multi-System)
> **Depends on:** [agent_systems_node_architecture.md](../agent_systems_node_architecture.md), [agent_systems_self_expansion_design.md](../agent_systems_self_expansion_design.md)
> **Affects:** `core/platform/`, `schemas/system_platform.py`, `core/agent_factory.py`, `tools/system_platform_tools.py`

---

## Table of Contents

1. [Problem Statement](#problem-statement)
2. [Core Idea](#core-idea)
3. [Target Model](#target-model)
4. [Layered Architecture](#layered-architecture)
5. [System-level Config Isolation](#system-level-config-isolation)
6. [Namespace & Tenant Model](#namespace--tenant-model)
7. [Federated Event Bus](#federated-event-bus)
8. [Cross-System Invocation](#cross-system-invocation)
9. [System Discovery & Dynamic Composition](#system-discovery--dynamic-composition)
10. [Security & Governance](#security--governance)
11. [Data Structures](#data-structures)
12. [Migration Plan](#migration-plan)
13. [Interaction with Existing Code](#interaction-with-existing-code)

---

## Problem Statement

### What we have today

| Capability | Status | Limitation |
|------------|--------|------------|
| Multiple agents | ✅ | All share one `AgentFactory` ↔ one `Config` |
| SystemDefinition graph | ✅ | `agent_ref` points to global `config.yaml` only |
| SystemRegistry | ✅ | Single JSON file, flat namespace |
| SystemRefNode (cross-system call) | ✅ | Only within the same registry |
| DomainEventBus | ✅ | Scoped to one `SystemRuntime` |
| User isolation (workspace) | ✅ | `workspace/user_{id}/*` |

### The core problem

```
                    ┌─────────────────────┐
                    │    config.yaml       │  ← ONE global config
                    │  agents: {...}       │
                    │  tools:  {...}       │
                    │  models: {...}       │
                    └─────────┬───────────┘
                              │
                    ┌─────────▼───────────┐
                    │   AgentFactory      │  ← ONE factory
                    └─────────┬───────────┘
                              │
                    ┌─────────▼───────────┐
                    │  SystemRegistry     │  ← ONE registry file
                    │  (flat namespace)   │
                    └─────────────────────┘
```

**Everything is a singleton.** A system cannot own its agents, tools, models, or registry namespace. This prevents:

- **True isolation** — system A's agents are visible to system B
- **Independent configuration** — a system can't bring its own `config.yaml` fragment
- **Cross-tenant boundaries** — no way to separate `tenant-1/dev` from `tenant-2/prod`
- **Composition of systems** — no way to wire two independently-developed systems without a human merging YAML

---

## Core Idea

> **A System becomes a self-contained deployable unit with its own config, namespace, and lifecycle — while still being discoverable and composable with other systems.**

Three principles:

1. **System-level Config** — Each `SystemDefinition` may carry an `embedded_config` (its own agents, tools, models). At runtime, the system's config is merged with the global config (system values take precedence).

2. **Namespaced Registries** — `SystemRegistry` gains a `namespace` dimension. A `RegistryHub` manages multiple registries. Systems in namespace `tenant-a` do not leak into `tenant-b`.

3. **Federated Execution** — Systems in different namespaces can discover, call, and subscribe to events from each other via explicit contracts (interface + channel), not via implicit shared state.

---

## Target Model

### Mental model

> A **System** is a versioned app. A **Namespace** is a deployment environment. The **RegistryHub** is a multi-tenant package registry. A **Channel** is a release pointer (`stable`, `canary`, `candidate`).

```
┌──────────────────────────────────────────────────────────────┐
│                      RegistryHub                             │
│                                                              │
│  ┌──────────────────────┐   ┌──────────────────────┐        │
│  │  Namespace "tenant-a"│   │  Namespace "tenant-b"│        │
│  │                      │   │                      │        │
│  │  SystemRegistry      │   │  SystemRegistry      │        │
│  │  ┌────────────────┐  │   │  ┌────────────────┐  │        │
│  │  │ code-reviewer  │  │   │  │ code-reviewer  │  │   ← same system_id,
│  │  │  v1.2.0 stable │  │   │  │  v1.0.0 stable │  │      different versions
│  │  ├────────────────┤  │   │  └────────────────┘  │      in different ns
│  │  │ doc-generator  │  │   │                      │
│  │  │  v0.9.0 canary │  │   │  ┌────────────────┐  │
│  │  └────────────────┘  │   │  │  artifact-bld  │  │
│  │                      │   │  │  v2.0.0 stable │  │
│  │  SystemRuntime       │   │  └────────────────┘  │
│  │  (tenant-a scope)    │   │                      │
│  └──────────────────────┘   │  SystemRuntime       │
│                              │  (tenant-b scope)    │
│                              └──────────────────────┘
│                                                              │
│  ┌──────────────────────────────────────────────────────┐   │
│  │            FederatedEventBus (cross-ns)              │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

### What a system can now own

| Entity | Before | After |
|--------|--------|-------|
| Agents | Referenced globally (`agent_ref: "agents.chat_agent"`) | Can define inline in `embedded_config` |
| Tools | Referenced globally or as local `.py` in bundle | Can define inline in `embedded_config` |
| Models | Shared from global `config.yaml.models` | Can override in `embedded_config.models` |
| Registry | One global JSON file | Per-namespace registry file |
| Events | Scoped to one runtime | Cross-namespace via `FederatedEventBus` |

---

## Layered Architecture

```
┌─────────────────────────────────────────────────┐
│ Layer 5: Discovery & Composition                │
│   system_search_systems(), system_compose()     │
│   CapabilityDirectory, SystemGraphComposer      │
├─────────────────────────────────────────────────┤
│ Layer 4: Federation                             │
│   FederatedEventBus, cross-ns subscription      │
│   SystemInterface contract enforcement          │
├─────────────────────────────────────────────────┤
│ Layer 3: Multi-Registry                         │
│   RegistryHub, namespaced SystemRegistry        │
│   Cross-registry SystemRefNode resolution       │
├─────────────────────────────────────────────────┤
│ Layer 2: System Config Isolation                │
│   embedded_config in SystemDefinition           │
│   Config merge: system ⊹ global                 │
│   Scoped AgentFactory per system invocation     │
├─────────────────────────────────────────────────┤
│ Layer 1: Execution (existing)                   │
│   SystemRuntime (proxy/graph modes)             │
│   SystemCompiler, Governance, Mutation           │
│   AgentFactory, Config                          │
└─────────────────────────────────────────────────┘
```

Layers 1-2 are per-invocation. Layers 3-5 are process-global singletons.

---

## System-level Config Isolation

### The problem with `agent_ref`

Today, an `AgentNodeDefinition` references an agent by its global key:

```yaml
# Inside SystemDefinition.nodes:
main:
  type: agent_node
  agent_ref: "agents.chat_agent"   # ← must exist in root config.yaml
```

If system `code-reviewer` wants a specialized `reviewer_agent` that exists only in that system's scope, there is nowhere to define it.

### Solution: `embedded_config`

Add an optional `embedded_config` field to `SystemDefinition`:

```python
class SystemDefinition(BaseModel):
    system_id: str
    version: str
    entrypoint: str
    interface: SystemInterface
    nodes: dict[str, ExecutableNode]
    edges: list[SystemEdge]
    policy: SystemPolicy
    default_agent: Optional[str] = None
    dependencies: list[str] = []

    # NEW — system-owned configuration fragment
    embedded_config: Optional[GridConfig] = None
```

`GridConfig` is the same Pydantic model already used for `config.yaml` (`schemas/schemas.py`). It contains:

```python
class GridConfig(BaseModel):
    agents: dict[str, AgentConfig] = {}
    tools: dict[str, ToolConfig] = {}
    models: dict[str, ModelConfig] = {}
    skills: dict[str, SkillConfig] = {}
    mcp_servers: dict[str, MCPServerConfig] = {}
    # ... settings, isolation, etc.
```

A system's `embedded_config` is a **partial** config — it only needs to declare what it adds or overrides.

### Config merge strategy

When `SystemRuntime` executes a system that has `embedded_config`, it performs a **layered merge**:

```
Runtime Config = global_config  ⊹  system.embedded_config
                 (base layer)      (override layer)
```

Merge rules:

| Field | Strategy |
|-------|----------|
| `agents` | Dict merge (system keys override global keys of same name) |
| `tools` | Dict merge |
| `models` | Dict merge |
| `skills` | Dict merge |
| `mcp_servers` | Dict merge |
| `settings` | Shallow merge |
| `agent_ref` resolution | First look in `embedded_config.agents`, then in `global_config.agents` |

### Agent resolution precedence

When resolving `agent_ref: "agents.reviewer_agent"`:

```
1. Look in system.embedded_config.agents["reviewer_agent"]   ← system-owned
2. Look in global_config.agents["reviewer_agent"]            ← global
3. Raise AgentNotFoundError
```

This allows a system to **shadow** a global agent definition while still having access to all global agents.

### Runtime factory scoping

`SystemRuntime` creates a **scoped AgentFactory** per system invocation:

```python
class SystemRuntime:
    def _execute_agent_node(self, node: AgentNodeDefinition, input_data: dict):
        # Build merged config for this system
        merged_config = self._build_merged_config(system_def)

        # Get or create a scoped factory
        factory = self._get_scoped_factory(
            system_id=system_def.system_id,
            config=merged_config,
        )

        # Run agent through scoped factory
        return factory.run_agent(agent_key=node.agent_ref, ...)
```

Scoped factories are **cached per (system_id, user_id, context_id)** and evicted when the system invocation completes or TTL expires.

### YAML representation

```yaml
system_id: code_reviewer
version: 1.2.0
entrypoint: review
interface:
  inputs:
    code: { type: string, required: true }
  outputs:
    report: { type: string }
    issues: { type: array }

embedded_config:
  agents:
    reviewer_agent:
      name: "Code Reviewer"
      model: "deepseek-v3-0324"
      system_prompt: "You are a thorough code reviewer..."
      tools: [file_read, grep_tool, orchestrator_tools__orchestrate]
      temperature: 0.2

    fixer_agent:
      name: "Auto Fixer"
      model: "claude-sonnet-4-20250514"
      system_prompt: "You fix code issues identified by the reviewer..."
      tools: [file_read, file_edit, bash_tool]

  tools:
    my_custom_tool:
      type: python
      module: "bundle_tools.review_helpers"
      function: "calculate_complexity"

  models:
    deepseek-v3-0324:
      provider: openrouter
      model: deepseek/deepseek-chat
      max_tokens: 16384

nodes:
  review:
    type: agent_node
    agent_ref: "agents.reviewer_agent"    # ← resolved from embedded_config first

  fix:
    type: agent_node
    agent_ref: "agents.fixer_agent"       # ← resolved from embedded_config

  report:
    type: tool_node
    tool_ref: "tools.my_custom_tool"      # ← resolved from embedded_config

edges:
  - from: review
    to: fix
    condition: "output.issues_count > 0"
  - from: fix
    to: report
  - from: review
    to: report
    condition: "output.issues_count == 0"

policy:
  max_time_seconds: 300
  max_tokens_total: 100000
  allow_tools: [file_read, file_edit, grep_tool, bash_tool]
```

---

## Namespace & Tenant Model

### Namespace definition

A **namespace** is a logical partition within the platform. Each namespace has:

- Its own `SystemRegistry` (separate JSON file)
- Its own set of release channels (`stable`, `canary`, `candidate`)
- Optional access control rules

Namespaces are **flat** — no hierarchy, no inheritance. Use naming conventions for grouping: `tenant-a/dev`, `tenant-a/prod`.

### RegistryHub

```python
class RegistryHub:
    """
    Manages multiple SystemRegistry instances, one per namespace.

    This is the single process-global entry point for all registry operations.
    """

    def __init__(self, base_path: str):
        self._base_path = Path(base_path)
        self._registries: dict[str, SystemRegistry] = {}

    def get_registry(self, namespace: str) -> SystemRegistry:
        """Get or create a registry for the given namespace."""
        if namespace not in self._registries:
            registry_path = self._base_path / namespace / "registry.json"
            self._registries[namespace] = SystemRegistry(
                registry_path=str(registry_path),
                namespace=namespace,
            )
        return self._registries[namespace]

    def list_namespaces(self) -> list[str]:
        """List all namespaces that have registry files."""
        ...

    def create_namespace(self, namespace: str) -> SystemRegistry:
        """Explicitly create a new namespace."""
        ...

    def delete_namespace(self, namespace: str) -> None:
        """Remove a namespace and all its systems (dangerous)."""
        ...
```

### Filesystem layout

```
data/platform/
├── hub.json                        # RegistryHub metadata (namespace index)
├── default/                        # namespace "default"
│   └── registry.json               # SystemRegistry for default
├── tenant-a/
│   └── registry.json
├── tenant-b/
│   └── registry.json
└── _global/                        # namespace for platform-internal systems
    └── registry.json
```

### Cross-namespace SystemRefNode

A `SystemRefNodeDefinition` can now optionally specify a `target_namespace`:

```python
class SystemRefNodeDefinition(BaseModel):
    target_system: str
    target_namespace: str = "default"   # NEW — defaults to caller's namespace
    target_channel: str = "stable"
    target_version: Optional[str] = None
```

Resolution order:

```
1. If target_namespace == caller's namespace → resolve locally (same as today)
2. Else → ask RegistryHub for target_namespace's registry → resolve target_system
3. If not found → raise SystemNotFoundError
```

### Invocation context propagation

When system A (in namespace `ns-a`) calls system B (in namespace `ns-b`) via `SystemRefNode`:

```python
class SystemInvocationContext:
    caller_system_id: str
    caller_namespace: str
    caller_user_id: str
    call_depth: int
    trace_id: str                  # For distributed tracing
    auth_token: Optional[str]      # For cross-tenant auth
```

The runtime passes this context so system B can enforce its own policies (e.g., «I only accept calls from namespace `ns-a`»).

---

## Federated Event Bus

### Why federation matters

Today, `DomainEventBus` is scoped to one `SystemRuntime`. Systems cannot react to events from other systems without explicit polling.

Use cases:
- System `code-reviewer` emits `review.completed` → System `ci-pipeline` reacts by running tests
- System `doc-generator` emits `docs.updated` → System `notification` sends a Slack message
- System `error-handler` subscribes to `*.error` across all namespaces

### FederatedEventBus

```python
class FederatedEventBus(DomainEventBus):
    """
    Extends DomainEventBus with cross-namespace publish/subscribe.

    Subscriptions can be:
    - Local (within same namespace) — same as today
    - Cross-namespace (explicit target namespace)
    - Global (all namespaces, wildcard "**")
    """

    def subscribe(
        self,
        event_type: str,
        handler: Callable,
        *,
        namespace: str = "local",
        source_system: Optional[str] = None,
    ):
        """
        Subscribe to events.

        namespace="local" — only events from caller's namespace
        namespace="tenant-b" — only events from that namespace
        namespace="**" — events from all namespaces (requires permission)
        """
        ...

    def publish(self, event: DomainEvent, *, namespace: str, source_system: str):
        """
        Publish an event to all matching subscribers (local + cross-ns).
        """
        ...
```

### Event routing table

| Subscriber scope | Publishes from `ns-a` | Publishes from `ns-b` |
|------------------|----------------------|----------------------|
| `namespace="local"` in `ns-a` | ✅ receives | ❌ does not receive |
| `namespace="ns-b"` in `ns-a` | ❌ | ✅ receives |
| `namespace="**"` in `ns-a` | ✅ | ✅ |
| `namespace="local"` in `ns-b` | ❌ | ✅ |

### Governance on cross-namespace events

Cross-namespace subscriptions require explicit permission in the subscribing system's `policy`:

```yaml
policy:
  allow_cross_namespace_events: true
  allowed_event_sources:
    - namespace: "tenant-a"
      system_ids: ["code-reviewer"]
    - namespace: "tenant-b"
      system_ids: ["**"]            # all systems in tenant-b
```

---

## Cross-System Invocation

### Four invocation paths

| Path | Mechanism | Use case |
|------|-----------|----------|
| **Agent-as-Tool** | `tools: [{type: agent, agent: "code_agent"}]` | Chat agent delegates to code agent |
| **orchestrate()** | Dynamic ad-hoc sub-agent creation | Unplanned sub-tasks |
| **SystemRefNode** | Static graph edge to another system | Predefined workflow across systems |
| **system_invoke_system()** | Tool-based cross-system call | Agent discovers and invokes at runtime |

The first two are **agent-level** (Layer 1, unchanged). The last two are **system-level** (Layer 3+) and extended by this design.

### system_invoke_system() — extended

Current signature (`tools/system_platform_tools.py`):

```python
def system_invoke_system(
    system_id: str,
    inputs: dict,
    *,
    channel: str = "stable",
    version: str = None,
) -> dict: ...
```

Extended signature:

```python
def system_invoke_system(
    system_id: str,
    inputs: dict,
    *,
    namespace: str = "default",          # NEW
    channel: str = "stable",
    version: str = None,
    max_call_depth: int = 3,              # NEW — per-call override
    timeout_seconds: int = 300,           # NEW
) -> SystemInvocationResult: ...

class SystemInvocationResult(BaseModel):
    outputs: dict
    system_id: str
    version: str
    namespace: str
    trace_id: str
    elapsed_ms: int
    node_traces: list[NodeExecutionTrace]
```

### Call depth tracking

The runtime already tracks call depth (`SystemPolicy.max_system_call_depth`). With cross-namespace calls, the depth counter is **global across namespaces**:

```
User → System A (ns-a, depth=0)
  → SystemRefNode → System B (ns-b, depth=1)
    → system_invoke_system → System C (ns-a, depth=2)
      → ERROR if depth > max_system_call_depth
```

The `SystemInvocationContext.call_depth` is incremented on every cross-system boundary, regardless of namespace.

---

## System Discovery & Dynamic Composition

### Capability-based discovery

Systems export their capabilities in `SystemInterface`:

```yaml
interface:
  inputs:
    code: { type: string }
    language: { type: string, default: "python" }
  outputs:
    report: { type: string }
    issues: { type: array }
  capabilities:                              # NEW
    - code_review
    - static_analysis
    - security_scan
  tags:                                       # NEW
    - python
    - production
```

### New tool: `system_search_systems()`

```python
def system_search_systems(
    query: str,                              # Semantic search query
    *,
    capabilities: list[str] = None,          # Filter by capability
    tags: list[str] = None,                  # Filter by tags
    namespace: str = None,                   # Limit to namespace (None = all)
    channel: str = "stable",
    limit: int = 10,
) -> list[SystemSearchResult]: ...

class SystemSearchResult(BaseModel):
    system_id: str
    namespace: str
    version: str
    channel: str
    description: str                         # From SystemDefinition.description
    capabilities: list[str]
    tags: list[str]
    interface: SystemInterface               # Input/output schemas
    score: float                             # Relevance score
```

This enables an agent to find systems by what they **do**, not by their ID.

### New tool: `system_compose()`

```python
def system_compose(
    systems: list[SystemRef],                # Systems to compose
    edges: list[CompositionEdge],            # How to wire them
    *,
    namespace: str = "default",
    composition_id: str = None,              # Auto-generated if not provided
) -> SystemDefinition: ...

class SystemRef(BaseModel):
    system_id: str
    namespace: str = "default"
    channel: str = "stable"
    alias: str                               # Local name in the composition graph

class CompositionEdge(BaseModel):
    from_alias: str
    to_alias: str
    input_map: dict[str, str]                # Map output keys → input keys
    condition: Optional[str] = None
```

Example of dynamic composition:

```python
# Agent calls this to wire up a review → fix → notify pipeline:
composed = system_compose(
    systems=[
        SystemRef(system_id="code-reviewer", namespace="shared", alias="review"),
        SystemRef(system_id="auto-fixer", namespace="shared", alias="fix"),
        SystemRef(system_id="slack-notifier", namespace="tenant-a", alias="notify"),
    ],
    edges=[
        CompositionEdge(from_alias="review", to_alias="fix",
                        input_map={"issues": "issues", "code": "code"}),
        CompositionEdge(from_alias="fix", to_alias="notify",
                        input_map={"report": "message"},
                        condition="output.fixed == true"),
    ],
)

# Result is a new SystemDefinition with:
# - Merged nodes from all three systems (prefixed by alias)
# - Injected edges between them
# - Entrypoint = first system in the list
# - Registered in the caller's namespace
```

The composed system is registered in the caller's namespace and can be invoked immediately.

### Composition at the graph level

`SystemCompiler` gains a `compose()` method:

```python
class SystemCompiler:
    def compose(
        self,
        systems: list[SystemDefinition],
        edges: list[CompositionEdge],
    ) -> SystemDefinition:
        """
        Merge multiple SystemDefinitions into one composite system.

        1. Prefix all node IDs with system alias (e.g., "review/main", "fix/main")
        2. Merge all edges from source systems
        3. Add composition edges (with input/output key mapping)
        4. Set entrypoint to first system's entrypoint
        5. Validate the resulting graph
        6. Merge policies (most restrictive wins)
        """
        ...
```

---

## Security & Governance

### Permission model (extended)

Existing governance (`core/platform/governance.py`) checks:

```yaml
policy:
  allow_tools: [file_read, grep_tool, ...]
  deny_tools: [bash_tool, ...]
  max_time_seconds: 300
  max_tokens_total: 100000
  max_system_call_depth: 3
```

New fields for multi-system:

```yaml
policy:
  # Cross-namespace call restrictions
  allow_cross_namespace_calls: true
  allowed_callers:                    # Who can call this system
    - namespace: "tenant-a"
      system_ids: ["**"]
    - namespace: "tenant-b"
      system_ids: ["ci-pipeline"]

  # Cross-namespace event restrictions
  allow_cross_namespace_events: true
  allowed_event_sources:
    - namespace: "tenant-a"
      system_ids: ["code-reviewer"]

  # Budget per cross-system call
  max_cross_system_call_cost: 10000  # tokens
  max_total_cross_system_cost: 50000 # tokens per invocation
```

### Caller identity propagation

Every cross-system call carries an `SystemInvocationContext` with:

```python
class SystemInvocationContext:
    caller_system_id: str
    caller_namespace: str
    caller_user_id: str
    caller_agent_id: str
    call_depth: int
    trace_id: str
    auth_token: Optional[str]
    # Signed by the calling runtime, verified by the receiving runtime
```

The receiving runtime validates:
1. Caller is in `policy.allowed_callers` (if restricted)
2. `call_depth` is within `policy.max_system_call_depth`
3. `auth_token` is valid (if cross-tenant auth is enabled)

### Budget tracking (extended)

`BudgetTracker` already exists in `governance.py`. It is extended to track per-namespace budgets:

```python
class BudgetTracker:
    def track_cross_system_call(
        self,
        caller_ns: str,
        target_ns: str,
        target_system: str,
        token_estimate: int,
    ) -> bool:
        """Returns True if within budget, False if exceeded."""
        ...

    def get_remaining_budget(self, namespace: str) -> dict:
        """Remaining budget per namespace."""
        ...
```

---

## Data Structures

### Full `SystemDefinition` with new fields

```python
class SystemDefinition(BaseModel):
    # Identity
    system_id: str
    version: str                                    # Semver, immutable
    description: Optional[str] = None                # NEW — human-readable
    namespace: Optional[str] = None                  # NEW — inferred from registry

    # Graph
    entrypoint: str
    nodes: dict[str, ExecutableNode]
    edges: list[SystemEdge] = []

    # Interface
    interface: SystemInterface

    # Configuration isolation
    embedded_config: Optional[GridConfig] = None    # NEW

    # Policy
    policy: SystemPolicy = SystemPolicy()

    # Dependencies
    dependencies: list[SystemDependency] = []        # Was list[str], now structured
    default_agent: Optional[str] = None

    # Metadata
    created_at: Optional[str] = None
    created_by: Optional[str] = None                 # NEW — agent_id that created this


class SystemDependency(BaseModel):                   # NEW
    system_id: str
    namespace: str = "default"
    channel: str = "stable"
    version: Optional[str] = None                    # Pin to specific version


class SystemInterface(BaseModel):
    inputs: dict[str, FieldSchema]
    outputs: dict[str, FieldSchema]
    capabilities: list[str] = []                     # NEW
    tags: list[str] = []                             # NEW


class SystemPolicy(BaseModel):
    allow_tools: list[str] = []
    deny_tools: list[str] = []
    max_time_seconds: int = 300
    max_tokens_total: int = 100000
    max_system_call_depth: int = 3
    allow_cross_namespace_calls: bool = False        # NEW
    allowed_callers: list[CallerRule] = []           # NEW
    allow_cross_namespace_events: bool = False       # NEW
    allowed_event_sources: list[EventSourceRule] = [] # NEW
    max_cross_system_call_cost: int = 10000          # NEW
    max_total_cross_system_cost: int = 50000         # NEW


class CallerRule(BaseModel):
    namespace: str
    system_ids: list[str]                            # ["**"] means all


class EventSourceRule(BaseModel):
    namespace: str
    system_ids: list[str]
```

### RegistryHub internal state

```python
class RegistryHubState(BaseModel):
    """Persisted in hub.json"""
    version: int = 1
    namespaces: dict[str, NamespaceMeta] = {}
    default_namespace: str = "default"

class NamespaceMeta(BaseModel):
    namespace: str
    registry_path: str
    created_at: str
    description: Optional[str] = None
    contact: Optional[str] = None
```

---

## Migration Plan

### Phase 1: embedded_config (non-breaking)

**Goal:** Systems can carry their own agents/tools/models. No registry changes.

1. Add `embedded_config: Optional[GridConfig]` to `SystemDefinition` (schema change)
2. Implement `Config.merge(base: GridConfig, overlay: GridConfig) -> GridConfig` in `core/config/config.py`
3. Modify `SystemRuntime._execute_agent_node` to resolve agents from merged config
4. Add scoped `AgentFactory` cache in `SystemRuntime` (keyed by `(system_id, user_id, context_id)`)
5. Write tests

**Impact:** Zero breaking changes. Existing systems without `embedded_config` work identically.

### Phase 2: Namespaced registries (non-breaking)

**Goal:** Multiple registries, namespaced access. RegistryHub as new singleton.

1. Create `core/platform/registry_hub.py` with `RegistryHub`
2. Add `namespace` parameter to `SystemRegistry.__init__`
3. Update `tools/system_platform_tools.py` to accept `namespace` in all tools
4. Old tools (without `namespace`) default to `"default"` — backward compatible
5. Create data directory structure `data/platform/{namespace}/registry.json`
6. Migrate existing `registry.json` → `data/platform/default/registry.json`

**Impact:** Backward compatible. The `default` namespace preserves existing behaviour.

### Phase 3: Federated events (opt-in)

**Goal:** Cross-namespace publish/subscribe.

1. Extend `DomainEventBus` → `FederatedEventBus`
2. Add `policy` fields for cross-namespace event permissions
3. Implement subscription routing table
4. Add `system_subscribe_event()` tool for agents

**Impact:** Opt-in. Systems must explicitly set `allow_cross_namespace_events: true`.

### Phase 4: Discovery & composition (new capabilities)

**Goal:** Agents can find and compose systems dynamically.

1. Implement `system_search_systems()` with semantic + tag filtering
2. Implement `SystemCompiler.compose()`
3. Implement `system_compose()` tool
4. Add capabilities/tags to `SystemInterface`
5. Build `CapabilityDirectory` index (in-memory cache of all registries)

**Impact:** All new functionality, no breaking changes.

---

## Interaction with Existing Code

### Files to modify

| File | Change | Phase |
|------|--------|-------|
| `schemas/system_platform.py` | Add `embedded_config`, `namespace`, `capabilities`, `tags`, new policy fields | 1–4 |
| `core/platform/runtime.py` | Scoped factory, merged config, cross-ns invocation context | 1–3 |
| `core/platform/registry.py` | Add `namespace` parameter, `list_namespaces()` | 2 |
| `core/platform/registry_hub.py` | **New file** — `RegistryHub` | 2 |
| `core/platform/events.py` | Extend to `FederatedEventBus` | 3 |
| `core/platform/compiler.py` | Add `compose()` method | 4 |
| `core/platform/governance.py` | Extend `PermissionChecker`, `BudgetTracker` | 1–3 |
| `core/config/config.py` | Add `Config.merge()` static method | 1 |
| `core/agent_factory.py` | Accept optional `GridConfig` override in constructor | 1 |
| `tools/system_platform_tools.py` | Add `namespace` param to all tools, new `system_search_systems`, `system_compose` | 2–4 |
| `web_chat/runtime.py` | Wire `RegistryHub` into service graph | 2 |

### Files NOT modified

| File | Reason |
|------|--------|
| `grid.py` (CLI) | No CLI changes needed — tools handle everything |
| `config.yaml` | Unchanged — all new config is in `embedded_config` |
| `core/memory/*` | Memory already isolated per user |
| `core/skills_integration.py` | Skills already isolated per workspace |
| `tools/orchestrator_tools.py` | `orchestrate()` works at agent level, unchanged |
| `core/meta_cognitive.py` | Meta-cognition operates on SystemDefinitions, which now carry more data |

### What remains single-instance (by design)

| Component | Reason to keep global |
|-----------|----------------------|
| `RegistryHub` | One hub coordinates all namespaces |
| `FederatedEventBus` | One bus routes all cross-ns events |
| Global `config.yaml` | Fallback defaults for all systems |
| `ModelManager` (OpenAI clients) | Connection pool shared across systems |

---

## Testing Strategy

### Unit tests

- `Config.merge()` — dict merge, shadowing, missing keys
- `SystemRegistry` with namespace — create, list, delete namespaces
- `RegistryHub` — get registry, cross-ns resolution
- `FederatedEventBus` — local, cross-ns, global subscriptions
- `SystemCompiler.compose()` — valid composition, prefix collisions, cycle detection

### Integration tests

- System A (with `embedded_config`) invokes system B (with its own `embedded_config`)
- Cross-namespace invocation with policy enforcement
- Dynamic composition of 3 systems into a pipeline
- Budget tracking across multiple systems
- Call depth limit enforcement

### Fixtures

```yaml
# tests/fixtures/multisystem/
#   system_with_embedded_config.yaml      — SystemDefinition with embedded_config
#   system_without_embedded_config.yaml   — Backward compat test
#   cross_namespace_caller.yaml           — System that calls across ns
#   cross_namespace_callee.yaml           — System that accepts cross-ns calls
#   composition_test_a.yaml               — For compose() tests
#   composition_test_b.yaml
```

---

## FAQ

**Q: Can a system in namespace A shadow a global agent and break namespace B?**

No. Each namespace has its own `SystemRegistry`. System A's `embedded_config` only affects invocations of system A. When system B runs, it uses its own `embedded_config` (or the global config if it has none).

**Q: What prevents one namespace from DoS-ing another?**

`BudgetTracker` tracks tokens per namespace. `SystemPolicy.max_total_cross_system_cost` limits calls from external systems. `max_system_call_depth` prevents infinite recursion.

**Q: Can I move a system from one namespace to another?**

Yes — export from namespace A, import into namespace B using `system_export()` / `system_import()` tools (TBD in Phase 2).

**Q: Does this replace `config.yaml`?**

No. `config.yaml` remains the **global default** that all systems fall back to. `embedded_config` is for overrides specific to a system.

---

## References

- [Agent Systems Node Architecture](../agent_systems_node_architecture.md) — original node-based system model
- [Self-Expansion Design](../agent_systems_self_expansion_design.md) — lifecycle and self-improvement
- [Cross-Layer Coordination](../cross_layer_coordination_and_agent_roles.md) — agent roles and event bus
- [Platform Code Structure](code_structure.md) — code map of `core/platform/`
- [Platform Current Status](current_status.md) — what's implemented vs planned
- [Platform User Algorithm](user_algorithm.md) — user-facing workflow
