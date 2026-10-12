# Integrated Grid review fixes

## Provenance

The fixes were developed on `fix/grid-review`, based on `251be73`, in the
isolated `.review-worktree`. `.review-overlay` remains a historical integration
sandbox. Neither worktree nor downloaded artifacts are part of these commits.

The current branch integrates those fixes with the pending footer-free contract
and `ModelConfig.prompt_cache` field. Integration was per-file, not a wholesale
replacement: models retain the schema field and capability checks; sessions
retain the safe no-op migration hook; tools retain footer-free results and
invocation-local context handoff. Regression expectations were updated to test
these contracts rather than destructive history migration or unconfigured
provider capability.

## Behavior and limitations

1. Responses-only summary models use streaming Responses, not Chat Completions.
   Summary model/fallback API options are resolved together. ChatGPT plan
   requests omit the API output cap and set `store=false`. Summary streams
   have a local character guard (four characters per configured output token,
   not an exact tokenizer-based budget). Error, failed, incomplete,
   missing-completion and empty streams are rejected; streams are closed.
2. A wrapped agent-tool invocation allocates its context ID once and hands it
   down out-of-band via a ContextVar. User-qualified sessions stay isolated,
   and results no longer need a model-visible footer.
3. Automatic persistent legacy cleanup is disabled, not made transactional.
   Text alone does not establish service provenance, and the SDK Session
   interface offers no atomic compare-and-replace. A future migration needs
   persisted provenance and an atomic storage API. Existing manual
   delete/compact clear-and-add behavior is outside this mitigation.
4. Prompt caching is gated to HTTPS OpenRouter Chat requests. Unknown/custom
   endpoints, Responses and ChatGPT plans omit `cache_control`, even with
   explicit opt-in. Transport tests inspect serialized JSON, not live cache
   hits, billing or latency.
5. Cancelling a synchronous tool's await does not release its user slot until
   the worker actually finishes. Python grep fallback uses confined no-follow
   file reads on supported platforms; its regressions target POSIX confinement.
   An rg timeout returns immediately instead of starting another timed search.
6. Deployment slices require Docker's systemd cgroup driver. Other drivers need
   a corresponding cgroup parent and deployment-specific limit setup.

## Verification of the integrated working tree

- Full Python suite: `python3 -m pytest -q -o addopts=''` — **2670 passed,
  3 skipped**, one existing Starlette/httpx deprecation warning.
- Full JavaScript suite: `node --test tests/*.test.mjs` — **233 passed**.
- Critical Python lint gate: Ruff `F,E9` on changed Python source/test files,
  excluding existing F811 imported-pytest-fixture warnings in `test_branches.py`.
- `git diff --check` passes.

Missing declared Python dependencies were installed in the test environment;
no dependency manifests were changed. Two environment-sensitive tests now
simulate root ownership explicitly and distinguish tool PATH checks from the
system-wide missing-Docker diagnostic. No fake Docker executable was used for
the final full-suite run.

Live provider caching and real Docker/systemd deployment remain unverified.
Review worktrees, reports and downloaded browser artifacts are preserved locally
and excluded from staging by explicit file lists.
