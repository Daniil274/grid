# Independent review: durable workflows (grid-next)

Status: **NOT ACCEPTED** — 6 defects (R1–R6) found; acceptance criteria for
`workspace-2rk` / `workspace-k78` are **not met**.

Scope: read-only review of `grid-next` workflow subsystem. No product code was
changed. No external models/network used. No `git commit`/`push`.

This report consolidates the findings already recorded in the bead notes for
`workspace-2rk` (defects R1–R6) plus the independent test evidence verified by
the coordinator. It does not re-run the full defect investigation.

## 1. Verified test evidence (coordinator, independent)

| Suite | Result | Notes |
|---|---|---|
| Workflow suite: `tests/test_workflow_{spec,journal,engine,reconcile,api,tool}.py` | **118 passed** | no models/network, tmp SQLite |
| Profile regression suite | **124 passed** | relevant existing regression tests |

These suites pass. They cover the fake-factory / unit paths, but they do **not**
exercise the real `AgentFactory` integration paths where R1–R3 manifest (see
severity rationale below). A green run on these suites is therefore **not**
sufficient for acceptance.

## 2. Full suite attempt (workspace-k78)

Command (single attempt, timeout 120s):

```
cd /workspace/grid-next && timeout 120 python3 -m pytest -q -o addopts="-q --tb=line"
```

Result: **collection aborted, no tests executed** (exit code 2).

```
ERROR collecting tests/test_web_attachments.py
  ModuleNotFoundError: No module named 'PIL'
ERROR collecting tests/test_web_fetch_ssrf.py
  ModuleNotFoundError: No module named 'aiohttp'
Interrupted: 2 errors during collection !!!
```

Interpretation: this is an **environment limitation** (missing optional
dependencies `Pillow`/`PIL` and `aiohttp`), not a product regression. Because
collection aborts at the first two modules, the full suite cannot produce a
pass/fail result in this environment. **No full-suite pass is claimed.**

Additional probe/suite work performed by the original verification agent was
never persisted; its results are **not recoverable** and are therefore **not
asserted** here.

## 3. Defects

Locations refer to files as of this review (line numbers as recorded in
`workspace-2rk` notes).

### R1 — HIGH: allowlist bypass in the real runtime path
`tools/function_tools.py:resolve_tool` (~l.192) resolves a **name** from the
global registry (`AVAILABLE_TOOLS` / `TOOL_ALIASES`) **without** checking
`agent_config.tools`. `core/factory/tools.py:_resolve_function_tools` (~l.297)
calls `resolve_tool` and only applies `confine_tools`/isolation.
`build_invoker` → `invoke` → `_resolve_step_tool` (`core/workflows/api.py`
~l.280) calls `factory._resolve_function_tools([step.tool])` with an arbitrary
name from the workflow spec.

Effect: a workflow step can name **any** system tool (`file_delete`, `git_*`,
`orchestrate`, …) that is not in the agent allowlist. The test
`test_workflow_run_refuses_a_tool_outside_the_allowlist` uses a fake factory, so
the real resolver's allowlist gap is unverified. The only bar is `ActionGate`
(effect policy) plus the effect/read cross-check.

### R2 — HIGH: `required_isolation` is never wired in production
`tools/workflow_tools.py:_service` (~l.108) calls `build_invoker(factory)`
without `required_isolation`; `api.py:build_invoker` defaults it to `None`, so
the confined-workspace block (~l.394–400) is **dead code** and never triggers.
`get_service` also does not set it.

### R3 — HIGH: `action_state` is never wired → steps denied under an active gate
`_service` calls `build_invoker(factory)` without `action_state`;
`_step_context` (`api.py` ~l.312) sets `action_state=None`.
`ActionGate.invoke` (`action_policy.py` ~l.857) reads
`ctx.context.action_state`; if it is not an `ActionRunState` it calls
`_deny('missing_trusted_task')` when the gate is enabled.

Effect: in real integration with an active policy, workflow steps **fail-closed
and do not execute**. The e2e tests use a fake factory without a gate, so this
is not covered.

### R4 — MEDIUM: `init_tools` are never executed
`WorkflowService.__init__` (`api.py` ~l.436–446) does not pass `init_invoke`.
`engine._run_init` (~l.701) sees `None` → `detail='no init runner configured'`;
a required init-tool run goes straight to `failed` (~l.327–334), optional →
degraded.

### R5 — MEDIUM: no heartbeat/lease renewal
`renew_lease` (`journal.py` ~l.358) is never called (grep count 0). A long run
(> lease TTL, e.g. 300s) in a multi-process setup: `reconcile` marks the run
interrupted, then the original engine fails on `set_run_status`
interrupted→completed (`RUN_TRANSITIONS`) with `InvalidTransition`.

### R6 — MEDIUM: step-result evidence is not atomic with step completion
`engine._finish_result` (~l.598) performs `finish_attempt` (tx1) and
`_persist_evidence` (tx2, ~l.672) in separate transactions. A crash between
them, followed by resume, uses the fallback `StepResult(ok=True, ref, data=None)`
→ **silent dataflow loss**.

## 4. Acceptance criteria (not met)

`workspace-2rk` acceptance: "independent report with actual tests and a concrete
list of defects or acceptance".

- Defects: **provided (R1–R6)** — this is a successful *report*.
- Acceptance of the **feature**: **NOT met**. R1–R3 are HIGH severity and
  affect the real `AgentFactory` path; the passing suites exercise fake-factory
  paths only.

Recommended conditions before acceptance:
1. R1–R3 fixed and covered by tests exercising the **real** factory resolver
   (allowlist enforcement, `required_isolation`, `action_state` propagation).
2. R4–R6 fixed or explicitly documented as out-of-scope with rationale.
3. Full suite runnable in CI with optional deps (`PIL`, `aiohttp`) present.

## 5. Repository state note

`/workspace/grid` (source) currently shows **16 modified tracked files** (vs 13
at the start of the original review) plus untracked `artifacts/`, `reports/`,
`tests/compact/...`. Byte-for-byte immutability is **not asserted**; these
changes are **not attributed** to any agent without evidence. Source was not
modified by this review.

## 6. Method / limits

- Read-only: **no product code changed**.
- No external models/network; local pytest + tmp SQLite only.
- R1–R6 taken from persisted `workspace-2rk` notes; investigation not repeated.
- Unpersisted probe results from the original agent: **unrecoverable, not asserted**.
- Full suite attempted **once** (timeout 120s); aborted at collection due to
  missing environment deps — recorded above, no pass claimed.
