# System builder

The web chat's system for creating systems. A user describes what a new system
should do; the builder designs its agents, writes its `config.yaml`, prompts,
skills and README from scratch, writes the tools it lacks as **tool packages**
with tests, checks the system the way the router does, runs package tests and evaluates
its acceptance scenarios in disposable containers with real models. The result is a **draft** on the systems page (`/systems`):
the owner tries it and activates it for their own router; an admin can publish
a shared draft. The builder never changes the lifecycle status.

## Who can use it

Every user. Admins build shared drafts in `systems/`; other users build private
drafts in `users/<id>/built_systems/`, outside the agents' workspace. The tools
receive only that user's `BuilderAccess` (`core/system_store.py`). A private
system is selectable by its owner and can be activated for that owner's router
on `/systems`; it is never published to other users. New Python tools use MCP
packages in the user's container. Private configs cannot import project tools
into the server or change the server's providers. On a one-user server the one
user is the admin.

## What it can do

| Tool | Does |
|---|---|
| `builder_catalog` | systems, models, shared tools, ready tool sets - what a system can be made of |
| `builder_tool_set` | the tools of a ready tool set and where each acts |
| `builder_read` | a file of a catalog system (an example) or a created one |
| `builder_create` | a new draft from a config written from scratch |
| `builder_write` / `builder_delete` | files of a draft: config, skills, tool packages, README |
| `builder_describe` | a draft's name, router description, required programs |
| `builder_check` | the router's health check, and what the tool packages hold |
| `builder_test_tools` | a package's tests in a throwaway container without network |
| `builder_evaluate` | real end-to-end scenarios, result assertions, time/token/call measurements |
| `builder_compare` | compare current reports on identical suites; list regressions separately |
| `builder_fork` | copy a system into a new draft, preserving the active version |

Shared published systems are read-only to it: a change goes through withdrawing
the system to a draft on the systems page. Active systems are changed through a new draft (`builder_fork`) or after withdrawal.

## New tools: tool packages

A tool the builder writes is a plain Python function with `@tool` in the
system's `tools/<package>/`, declared as an MCP tool with `tool_package:`
(`core/tool_packages/`). The server only reads it: its code runs in the user's
container, started as an MCP server by the runtime `grid_tool.py`, with its
pinned PyPI requirements installed there - so it gets no more than the agents'
shell commands already have, and the action policy judges every call. System
packages (apt) are not installed this way; a system that needs them says so.

## Example request

> Сделай систему для анализа текстов: статистика слов и предложений по файлам
> в рабочей папке, поиск повторов, краткий отчёт.

## Quality workflow

The builder writes `quality.yaml` alongside the config. It specifies the result,
acceptance criteria, architecture rationale, every agent's input/output/failure
contract, usage examples, limitations, and bounded scenarios. See
[the quality skill](skills/quality.md) for a complete example and assertion syntax.
The builder implements role contracts in prompts/skills; the metadata alone does
not impose typed runtime handoffs.

`builder_check.healthy` remains a configuration check. Its `quality` field reports
`not_configured`, `invalid`, `not_tested`, `stale`, `failed`, or `passed`, as well as
current package-test evidence, design warnings and `ready`. A ready system passed
all assertions in every repeat, has current nonempty offline package tests where
needed, and has no design errors. This is evidence for those scenarios only.

On Systems → Test, the owner can add independent acceptance cases. The web API
`PUT /api/systems/{created|built}/{key}/acceptance` accepts `{yaml: ...}` containing
`scenarios` and optional `repetitions` using the same scenario schema. This also
supports fixtures and multiple assertions. These cases are stored outside the
builder's file access; the builder cannot edit their expected outputs. Forks copy
the owner cases unchanged, but never copy passing reports. Cases are independent
in authorship, not a secret benchmark: failed-run diagnostics can inform repairs.

Every evaluation snapshots the candidate, then runs each scenario in a fresh
workspace with only its fixtures. Assertions and reports are not mounted into
the agent's container. Generated Python executes only as container tool packages.
Commands run as the image's agent user, with resource limits and no capabilities.
Pinned dependencies are installed before the agent starts, then the container's
network is disconnected. Models and trusted function tools still run through the
server, under its action policy; this does not make network-enabled shared tools
offline. The runner requires Docker and never falls back to running commands on
the host. Reported required programs are checked inside the sandbox.

Reports and package-test evidence live in `<store>/.quality/<key>/`, outside the
candidate directory. They are tied to a content digest including config, prompts,
tools, contract and routing requirements. Changing owner cases also invalidates
evaluation evidence. Status/name changes do not require retesting. Once a quality
contract is written via the builder or evaluated, removing the file cannot bypass
the activation check. Older systems without a contract remain compatible and
are explicitly shown as not configured for quality checks.

Publication/activation checks require current evidence for enrolled systems.
They remain an owner/admin action. Active behavior cannot be edited through the
builder or config API: fork a candidate, evaluate it, compare it with the original,
then switch routing explicitly. The original remains available for rollback.
The builder stops after at most three repair/evaluation cycles and reports a
remaining failure or infrastructure blocker instead of claiming success.

## Limits of the initial evaluator

- Local synchronous `type: agent` delegation, shared confined tools and local MCP
  tool packages are supported. Imported `project_tools`, arbitrary MCP commands,
  background/cross-system orchestration and live container-network integrations
  are refused explicitly.
- Token and tool-call limits apply to observed events across nested agents. A
  request already in flight can overshoot a limit before usage arrives. These
  are not hard billing quotas; policy/compaction calls and provider retries may
  add unreported cost. No currency cost is fabricated. Missing usage
  fails the scenario's budget verification.
- A scenario deadline includes environment setup and agent execution. Cleanup
  has a bounded additional allowance. Each repeat starts fresh; testing a retry
  or resumption requires a scenario that performs it within that run.
- Scenario assertions cover text, exact JSON/CSV, file existence and preserved
  inputs. Semantic prose quality, interactive usability and real external
  integration behavior still need owner review. Routing uses the existing router
  probe on Systems, separately from these pinned-system execution scenarios.
- Content hashes do not freeze remote model implementations or container tags.
  Pin dependencies/images and repeat evaluations when the runtime changes.

## Validation

`pytest -q tests/test_system_quality.py tests/test_system_builder.py
 tests/test_system_hub.py tests/test_tool_packages.py` covers ownership, protected
acceptance cases, stale evidence, comparisons, forks, failed budgets, timeouts,
missing metrics and sandbox cleanup. The package sandbox integration test uses
Docker when `grid-agent:latest` is installed. Frontend tests use Node 22.
