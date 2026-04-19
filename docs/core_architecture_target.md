# Core Architecture Target

This document defines the target architecture for `core/` and the migration
rules we should follow while refactoring.

## Goals

- Keep a small, stable public API.
- Separate domain logic from infrastructure and SDK adapters.
- Make dependencies flow inward only.
- Reduce god objects, especially `AgentFactory`.
- Preserve backward compatibility while moving code.

## Architectural Layers

### 1. Domain

Pure business logic and rules. No OpenAI SDK, filesystem, subprocess, SQLite,
or network concerns.

Target modules:

- `core/platform/`
- `core/cognition/`
- `schemas/`

Rules:

- Domain code depends on `schemas` and stdlib only.
- Domain services must be deterministic and easy to unit test.
- Domain events are semantic, not transport-specific.

### 2. Application

Use-case orchestration that coordinates domain services and ports.

Target modules:

- `core/application/` for future use cases such as:
- `agent_runtime.py`
- `context_assembly.py`
- `system_building.py`
- `memory_maintenance.py`

Rules:

- Application services may depend on domain modules and abstract ports.
- Application services must not depend directly on concrete SDK/tooling code.
- Cross-cutting workflows should move here instead of accumulating in
  `AgentFactory`.

### 3. Ports

Abstract interfaces that define what infrastructure must provide.

Target modules:

- `core/ports/`

Examples:

- `ModelGateway`
- `ToolCatalog`
- `MemoryRepository`
- `TracePublisher`
- `SessionStore`
- `SkillRepository`

Rules:

- Ports are small and capability-oriented.
- Avoid “manager” interfaces that expose broad mutable state.

### 4. Infrastructure

Concrete implementations for storage, SDK integration, tracing, MCP, and
project tool loading.

Target modules:

- `core/config/`
- `core/memory/`
- `core/tracing/`
- `core/managers/` only as temporary adapters during migration

Rules:

- Infrastructure implements ports.
- Infrastructure may depend on external SDKs and the filesystem.
- Infrastructure must not become the source of business rules.

### 5. Interfaces

Entry points exposed to users, examples, transports, and tools.

Examples:

- `agent_chat.py`
- `grid.py`
- `examples/telegram_bot/`
- `tools/`
- `web_chat/`

Rules:

- Interface code talks to application services, not directly to deep
  infrastructure internals.
- UI/transports must not instantiate half the system ad hoc.

## Public API Rules

There are only three supported import surfaces for long-term use:

- `core`
- `core.platform`
- `core.cognition`

Additional structured surfaces are acceptable when explicitly documented:

- `core.config`
- `core.memory`
- `core.improvement`
- `core.tracing`

Flat legacy modules such as `core.system_runtime` or `core.memory_store` should
remain as compatibility aliases until all internal and external consumers are
migrated.

## Agent Runtime Decomposition

`AgentFactory` should be reduced to composition and lifecycle wiring.

Target split:

- `AgentRuntimeFacade`: public orchestration entry point
- `AgentBootstrapper`: builds agents and runtime dependencies
- `AgentExecutionService`: runs requests and coordinates streams/tools
- `ContextAssemblyService`: builds model-facing instructions
- `ToolResolutionService`: resolves function tools, agent tools, and MCP tools
- `RunStateTracker`: owns execution/session/pipeline state

Rules:

- No new feature should be added directly to `AgentFactory` unless it is purely
  composition code.
- Feature logic should land in a dedicated service first.

## Dependency Rules

Allowed:

- interfaces -> application
- application -> domain
- application -> ports
- infrastructure -> ports
- infrastructure -> domain

Disallowed:

- domain -> infrastructure
- domain -> OpenAI SDK
- infrastructure -> interface layer
- broad circular imports between managers and factory classes

## Migration Strategy

1. Keep compatibility aliases for old imports.
2. Introduce explicit ports before moving large behaviors.
3. Extract one use case at a time from `AgentFactory`.
4. Move `core/managers/*` into either `application/` or `infrastructure/`
   depending on their real responsibility.
5. Add tests around public behavior before deleting compatibility layers.

## Immediate Priorities

1. Stabilize import compatibility and docs.
2. Introduce `core/application/` and `core/ports/` packages.
3. Extract tool resolution from `AgentFactory`.
4. Extract runtime execution/session state from `AgentFactory`.
5. Split memory storage from memory policies/maintenance jobs.

