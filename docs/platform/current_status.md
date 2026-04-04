# Current Status

This page tracks what is already implemented in the self-organizing platform MVP
and what remains as the next layer of work.

## Implemented now

### Execution foundation

- `ExecutableNode`, `SystemDefinition`, `SystemRefNodeDefinition`,
  `SystemInterface`, and `SystemRunResult` models exist.
- Systems can be resolved by `system_id + version` and `system_id + channel`.
- `SystemRuntime` supports:
  - proxy execution mode;
  - basic graph execution mode;
  - downstream node input enriched with upstream node outputs;
  - nested system invocation through `SystemRefNodeDefinition`;
  - max-depth checks through runtime context and policy.

### Compiler and safety

- The compiler validates:
  - entrypoint presence;
  - edge references;
  - predicate AST shape;
  - graph cycles;
  - cross-system dependency cycles.
- Edge conditions use declarative predicate ASTs and are evaluated by
  `ConditionEvaluator`.
- No string-based `eval` is used for runtime branching.

### Registry and release lifecycle

- System versions are immutable records.
- Release channels are mutable pointers.
- Candidate and canary promotion flow exists.
- Stable promotion uses compare-and-swap semantics and can reject channel
  conflicts.

### Governance and bounded mutation

- Permission roles and budget policy models exist.
- `BudgetTracker` and permission checks are wired into the platform layer.
- `DraftSystemBuilder`, `MutationSet`, and `SystemMutator` provide domain-level
  mutations instead of raw config editing.

### Knowledge and meta-cognitive layer

- Task ontology, capability registry, semantic contracts, pattern registry,
  reflection store, and template instantiation exist as MVP services.
- Coverage records, semantic drift checks, and lifecycle health heuristics are
  available.

### Tool surface

- Agents can now:
  - list registered systems;
  - inspect manifests and versions;
  - invoke systems through the platform runtime;
  - create versions from full definitions;
  - clone existing versions;
  - apply bounded mutation sets;
  - promote to canary/stable;
  - reject versions and rollback stable.
- The builder loop is also exposed as `system_build_bundle`, so an agent can
  request a new or improved bundle through the shared platform tool surface.
- These tools are registered in the global function-tool registry.

### Prototype lifecycle

- `SystemWorkbench` now provides a practical MVP lifecycle service for:
  - create;
  - clone;
  - mutate;
  - promote;
  - reject;
  - rollback.
- Platform registry path is now configurable through `settings.platform.registry_path`.
- Concrete bundle examples now live under `examples/platform_systems/`:
  - each system bundle keeps its local tools next to its config artifacts;
  - the case-example builder produces a runnable registry and invocation results.

## Still partial

- Sandbox enforcement is modeled, but not yet backed by a real isolation backend.
- Event subscribers exist as a bus abstraction, but derived indexes do not yet run
  as a full reactive coordination mesh.
- Consolidation and multi-metric evaluation are still heuristic scaffolds.
- Pattern extraction and capability composition are not yet automated end-to-end.
- The platform is file-backed today; the domain model is ready for a transactional
  backend, but the backend swap itself is still pending.

## Recommended next work

1. Connect registry/runtime paths to project configuration so the platform becomes
   a first-class runtime subsystem rather than an isolated MVP module.
2. Expand the bounded tool surface from the current lifecycle operations to richer
   review, governance, and policy-specific actions.
3. Add event-driven subscribers for coverage rebuilds, drift monitoring, audit
   trails, and consolidation suggestions.
4. Introduce stronger tests around permissions, budgets, and nested execution
   failure semantics.
5. Move candidate execution from policy-only sandbox declarations to real isolated
   execution profiles.
