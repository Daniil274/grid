# System Builder Prototype

This page documents the missing layer between the platform foundation and the
user goal:

- user gives a natural-language request;
- a builder model creates a new system bundle or improved version;
- the platform writes bundle files;
- the platform registers a candidate version;
- the platform runs a test invocation;
- the platform prepares review artifacts;
- the version can then be promoted to `stable`.

## Current prototype

The builder prototype lives in:

- [system_builder.py](/c:/Users/danii/grid/core/system_builder.py)
- [platform_system_builder_live.py](/c:/Users/danii/grid/examples/platform_system_builder_live.py)
- [system_builder_tools.py](/c:/Users/danii/grid/tools/system_builder_tools.py)

It uses `kimi-k2.5-opencode` as the builder model.

## Output bundle

Each generated bundle is written under `workspace/generated_systems_live/...`
and contains:

- `request.txt`
- `builder_bundle.json`
- `system_definition.json`
- `local_tools.py`
- `review_instructions.md`
- `candidate_run.json`
- `builder_report.json`

## Important scope

This is a prototype builder loop, not the final autonomous production system.

What it already does:

- accepts natural-language build requests;
- generates bundle specs with a live model;
- writes a new config and local tools;
- registers a candidate version in the platform registry;
- runs a real candidate invocation;
- prepares review artifacts for user testing;
- can optionally auto-promote.
- exposes the builder loop as the `system_build_bundle` platform tool.

What is still intentionally limited:

- generated agent refs are constrained to a small supported set;
- generated local tool code is limited to stdlib-only helpers;
- the user review step is represented by review artifacts and optional auto-promotion,
  not yet by a full interactive UI approval workflow.
