# Case Examples

This page shows two concrete platform scenarios derived from the original user
requirements.

Bundle rule:

- system-local tools stay next to the system bundle config
- example bundle configs live in JSON files inside the same bundle folder
- global `tools/` should only contain shared platform tools, not per-system helpers

## Example 1. Improve an existing system

Target:

- improve `claude_tools_system`
- add orchestration
- add testing

Implementation shape:

- base version `0.1.0` contains one `agent_node`
- version `0.2.0` is created through bounded mutation
- bundle files:
  - [base_system.json](/c:/Users/danii/grid/examples/platform_systems/claude_tools_plus/base_system.json)
  - [improvement_mutations.json](/c:/Users/danii/grid/examples/platform_systems/claude_tools_plus/improvement_mutations.json)
- new nodes:
  - `orchestrator` as `agent_node`
  - `tester` as `tool_node`
- new tool is local to the system bundle:
  - [local_tools.py](/c:/Users/danii/grid/examples/platform_systems/claude_tools_plus/local_tools.py)

Meaning:

- this is an `improvement` flow
- same system family
- new version

## Example 2. Create a new system with new tools

Target:

- create `artifact_delivery_system`
- create local tools for bundling and validation
- create a new graph-based system on top of them

Implementation shape:

- system-local tools live in:
  - [local_tools.py](/c:/Users/danii/grid/examples/platform_systems/artifact_delivery/local_tools.py)
- system config lives in:
  - [system_definition.json](/c:/Users/danii/grid/examples/platform_systems/artifact_delivery/system_definition.json)
- graph:
  - `planner` agent node
  - `bundle` tool node
  - `validator` tool node

Meaning:

- this is an `expansion` flow
- new system family
- new version `0.1.0`

## Builder script

The full reproducible example is:

- [build_case_examples.py](/c:/Users/danii/grid/examples/platform_systems/build_case_examples.py)

It:

1. creates a platform registry in `workspace/platform_case_examples`
2. registers the base `claude_tools_system`
3. mutates it into an improved stable version
4. creates the new `artifact_delivery_system`
5. executes both through `SystemRuntime`
6. writes `results.json`

This is the preferred example of how to keep system-local tools next to system
configs and artifacts instead of polluting the global source tree.
