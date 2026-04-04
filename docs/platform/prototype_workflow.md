# Prototype Workflow

This is the current end-to-end MVP workflow for the self-organizing platform.

## What the prototype can do now

The platform now supports a bounded lifecycle:

1. Create a new system version from a full `SystemDefinition`.
2. Clone an existing version into a new candidate.
3. Apply bounded domain mutations to produce a new version.
4. Send a version to `canary`.
5. Promote a version to `stable`.
6. Reject a candidate.
7. Roll back `stable` to a previous version.
8. Invoke registered systems through the runtime.

## Main runtime path

### Services

- `core/platform/registry.py`
- `core/platform/runtime.py`
- `core/platform/mutation.py`
- `core/platform/release.py`
- `core/platform/workbench.py`

### Tool surface

- `system_list_systems`
- `system_get_system_info`
- `system_get_system_versions`
- `system_invoke_system`
- `system_create_version`
- `system_clone_version`
- `system_apply_mutations`
- `system_promote_version`
- `system_reject_version`
- `system_rollback_stable`

## Configuration

The platform can now be wired through `settings.platform`:

```yaml
settings:
  platform:
    enabled: true
    registry_path: data/system_registry.json
    pattern_registry_path: data/pattern_registry.json
    default_actor_role: builder_agent
```

At runtime, `Config.get_system_registry_path()` resolves the effective absolute
path for the registry.

## Current operating model

### Builder flow

- Builder creates a draft or candidate version from a full definition.
- Builder can derive the next version by clone or mutation instead of editing raw
  registry state.
- Compiler validates the produced definition before registration.

### Reviewer flow

- Reviewer moves a candidate to `canary`.
- Reviewer promotes a canary to `stable`.
- Reviewer can reject or roll back a version.

### Runtime flow

- Runtime resolves a system by channel or pinned version.
- Runtime executes it in proxy mode or graph mode.
- Nested system calls are explicit through `SystemRefNodeDefinition`.

## Live smoke runner

There is now a dedicated live smoke script:

- `examples/platform_smoke_runner.py`

It does the following:

1. loads `OPENCODE_API_KEY` from `.env` if present;
2. creates a temporary registry with `demo_platform_system`;
3. runs the core platform operations deterministically in Python;
4. builds a temporary config with a narrow `platform_reporter` agent on
   `kimi-k2.5-opencode`;
5. asks the live model to summarize the factual smoke-test results.

Example:

```bash
python examples/platform_smoke_runner.py --config config.yaml
```

This is the preferred way to do a live MVP smoke test without hand-assembling a
temporary config each time.

## MVP boundaries

This is a real working prototype, but still intentionally bounded:

- no full reactive event-driven derived indexes yet;
- no real sandbox backend yet;
- no automated pattern extraction pipeline yet;
- no fully automated governance coordinator yet.

The key change is that the platform is no longer only a schema/runtime
foundation. It now has a usable controlled lifecycle loop.
