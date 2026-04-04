# Code Structure

This page maps the self-organizing platform design to concrete code locations.

## Layer Map

### Execution and control

- `schemas/system_platform.py`
  Source-of-truth models for executable nodes, system definitions, release state,
  permissions, budgets, runtime results, and governance decisions.
- `schemas/platform/__init__.py`
  Structured schema exports for platform-facing imports.
- `core/system_compiler.py`
  Minimal compiler for graph validation, predicate validation, and dependency cycle
  checks.
- `core/system_registry.py`
  Versioned registry, manifests, release channels, and registry persistence.
- `core/system_release_manager.py`
  Lifecycle transitions, canary flow, and stable promotion logic.
- `core/system_runtime.py`
  Proxy mode and basic graph mode execution.
- `core/system_governance.py`
  Permission checks, budget tracking, and governance helpers.
- `core/condition_evaluator.py`
  Safe predicate AST evaluation without string `eval`.
- `core/system_mutation.py`
  Domain mutations for draft and candidate system construction.
- `core/event_bus.py`
  Domain event transport used by registry and future derived indexes.

### Structured platform entrypoints

- `core/platform/__init__.py`
  Stable import surface for execution/control services.
- `core/platform/*.py`
  Thin wrappers that keep the new structure discoverable without breaking the
  earlier flat imports.

### Knowledge and meta-cognition

- `schemas/meta_cognitive.py`
  Task, capability, semantic identity, pattern, template, reflection, and health
  models.
- `schemas/cognition/__init__.py`
  Structured schema exports for cognition-facing imports.
- `core/meta_cognitive.py`
  Registries and services for ontology, patterns, reflection, template
  instantiation, coverage, drift, and lifecycle health.
- `core/cognition/__init__.py`
  Stable import surface for knowledge and meta-cognitive services.
- `core/cognition/*.py`
  Structured wrappers for knowledge, pattern, and health-oriented APIs.

### Tool surface

- `tools/system_platform_tools.py`
  Bounded tool API for listing systems, inspecting versions, and invoking systems
  through the runtime.
- `tools/function_tools.py`
  Global tool registry and alias map. This is where the platform tools are made
  available to agents.
- `tools/__init__.py`
  Public tool exports for direct imports.

### Tests

- `tests/test_system_platform.py`
  MVP coverage for compiler safety, registry/versioning, runtime modes, mutation,
  template instantiation, reflection, coverage, and drift detection.

## Structural conventions

- `schemas/` holds canonical typed domain models.
- `core/` holds implementations and orchestration services.
- `core/platform/` and `core/cognition/` are the preferred structured import
  surfaces for new code.
- `tools/` exposes only bounded APIs; agents should not mutate platform state by
  editing raw registry files.
- `docs/platform/` is the navigation hub for platform-specific documentation.

## Recommended import style for new code

Prefer:

- `from core.platform import SystemRegistry, SystemRuntime, SystemMutator`
- `from core.cognition import TaskOntology, PatternRegistry, ReflectionStore`
- `from schemas.platform import SystemDefinition, SystemRunResult`
- `from schemas.cognition import TaskType, StrategyPattern, DesignTemplate`

Avoid introducing new direct imports from the older flat modules unless the code
is extending those modules themselves.
