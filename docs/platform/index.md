# Platform Architecture

This section documents the new self-organizing system platform layer.

## Contents

1. [System Node Architecture](../agent_systems_node_architecture.md)
2. [Self-Expansion Design](../agent_systems_self_expansion_design.md)
3. [Meta-Cognitive Layer](../meta_cognitive_layer_design.md)
4. [Coordination And Agent Roles](../cross_layer_coordination_and_agent_roles.md)
5. [Code Structure](code_structure.md)
6. [Current Status](current_status.md)
7. [Prototype Workflow](prototype_workflow.md)
8. [User Algorithm](user_algorithm.md)
9. [Case Examples](case_examples.md)
10. [System Builder Prototype](system_builder_prototype.md)

## Code Structure

- `schemas/system_platform.py`: execution/control domain models
- `schemas/meta_cognitive.py`: knowledge and meta-cognitive domain models
- `core/platform/`: structured entrypoint for execution/control services
- `core/cognition/`: structured entrypoint for knowledge/meta-cognitive services
- `tools/system_platform_tools.py`: bounded tool surface for registry/runtime access
- `examples/platform_systems/`: example system bundles with colocated configs and local tools

For a file-by-file map, use [code_structure.md](code_structure.md).
For the currently implemented lifecycle loop, use [prototype_workflow.md](prototype_workflow.md).
For the practical operator flow, use [user_algorithm.md](user_algorithm.md).

## Current MVP

Implemented now:

- system registry and release state
- proxy and basic graph runtime
- safe predicate evaluation
- permission and budget checks
- bounded domain mutation layer for draft/candidate systems
- task/capability/pattern/template/reflection scaffolding
- coverage, drift, and lifecycle health helpers
- bounded platform tool surface wired into the global tool registry

Planned next:

- config integration
- richer governance and event subscribers
- automated pattern extraction and consolidation flows

For implemented scope and remaining gaps, use [current_status.md](current_status.md).
