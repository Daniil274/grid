# Changelog

All notable changes to Grid are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions use
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-23

Revision safety foundation for stored conversations. **Prepared, not activated:**
this entry describes what the 0.2.0 code implements. No server restart, image
publication or configuration change was performed as part of preparing it; the
update is activated by the user after a backup and review
(`docs/release-process.md`).

### Added

- `core/revisions.py`: immutable, content-addressed revision manifest per
  conversation, pinned on the first turn that writes into it and stored with the
  conversation. A manifest carries the Grid version, runtime (Python minor
  series, implementation, OS), four independently versioned contracts and a
  secret-free projection of the resolved system configuration.
- Four separate contracts, so their compatibility questions stay distinct:
  `history` (serialized history fields), `checkpoint` (a workspace-before-turn
  snapshot that resumes workspace files only), `runtime` (JSON serialization)
  and `tools` (stored tool-call semantics; permissions stay with the action
  policy).
- Fail-closed compatibility gate before routing, warm-up and any external
  operation, shared by web chat, lazy agents and the CLI (`core/factory/turns.py`).
  Unknown conversations and context managers without a revision API are refused;
  `revision_trusted_ephemeral = True` is the only explicit opt-out for callers
  that keep no conversations.
- Machine-readable refusal reasons (`revision.*` codes) on
  `ContextCompatibilityError`, carrying the context id and what differed, with
  no secrets in the message or details.
- Package-relative external material in the system digest: instruction files of
  system skills and source trees of local tool packages (`tool_package`,
  `project_tools`). Editing such a file refuses continuation just like editing
  `config.yaml`.
- `tools/release_check.py`: offline, side-effect-free validator that compares
  `releases/manifest.json` with `core/revisions.py` and `core.__version__`, with
  a human summary and `--json` output.
- `releases/manifest.json`: machine-readable release record (`schema_version` 1),
  currently `prepared`, describing 0.2.0 and its contracts.
- `docs/revisions.md` (what is pinned, compatibility, refusals, legacy, privacy,
  limits) and `docs/release-process.md` (publication criteria, safe update and
  rollback).
- `core/migration.py`: **reference-only bootstrap** (`action =
  "context.reference_bootstrap"`, `version = 1`) as the one supported way
  forward for a conversation that cannot continue here - legacy (no manifest)
  or pinned to another Grid version, runtime or resolved system. It creates a
  **new empty** conversation pinned to this environment and records an explicit,
  machine-readable reference to the old one; nothing is adopted, replayed or
  executed. The source is never written, re-pinned, edited or deleted and keeps
  its own state byte for byte; the record states its provenance honestly -
  `unknown` for a legacy conversation, the pinned revision (version and digest)
  for an intact but incompatible one. Messages, executions, agent SDK sessions,
  queued messages, a pending run, checkpoints, workspace files, approvals,
  credentials and the action policy are **not** imported: they are named with
  counts only, and history is referenced by digest, never loaded as executable
  state. Dry run first (writes nothing, returns a token bound to the exact source
  content, manifest and metadata); applying needs that token **and**
  `confirm=true`, recomputes the plan under a process lock, and refuses a changed
  source as `migration.confirmation_stale`. The target's pin and record are
  stored strictly before success, so a failed pin leaves no half-migrated
  conversation; a repeated apply in the same environment returns
  `migration.completed` for the same target (idempotent). A target whose
  configuration no longer resolves is `migration.target_incompatible`: it is
  never re-pinned, and the safe next step is to bootstrap from that target.
  Future-format, malformed, tampered and unknown-integrity records are refused
  fail-closed.
- Web chat API: `GET /api/chat/conversations/{id}/migration` (dry run) and
  `POST /api/chat/conversations/{id}/migration` with
  `{"plan_token": "...", "confirm": true}` (`MigrationRequest`), owner-scoped
  and holding the conversation's turn slot while it runs.
- Web chat UI: the rail's "Reference-only copy…" action runs the preview and
  states what is never imported or restored before the owner ticks consent and
  sends the bound token; the new empty chat carries a persistent banner with a
  link to the source, so an old thread can be read and copied by hand - it is
  not handed to the agent automatically. `web_chat/space.py` never prunes an
  empty conversation that is a bootstrap target.
- CI: a `release` job runs the offline validator before installing
  dependencies, checks that the checkout holds no `.env`, and runs the focused
  revision tests. CD additionally requires the release tag to match the
  manifest's `current` version.

### Changed

- `core/__init__.py`: version `0.1.0` -> `0.2.0`.
- `core/context.py`: conversations persist and load their revision manifest in
  both the JSON file and the encrypted conversation store; pinning is immutable
  (an existing manifest is never replaced, a legacy conversation is never
  adopted); branching a conversation copies its manifest under the new context
  id.
- **Exact, conservative equality** for continuation: Grid version, supported
  manifest and contract versions, exact contract digests, Python minor series
  and exact resolved-configuration digest. Any difference refuses; a matching
  secret-free projection does not make a conversation compatible.
- Permissions remain authoritative in the action policy for every turn, pinned
  or not. A manifest never grants or widens access.
- Secrets never enter a manifest: prompts, credentials, environment values and
  absolute paths are not stored. Only a projection (agent/model/tool keys,
  providers, context sizes, resolved limits, policy mode and file name) is kept;
  digest material is redacted, credential-looking files are never read, and
  source files are hashed in full (sha256) - there is no size-only
  fingerprinting. Rotating a credential does not invalidate a conversation;
  changing a prompt does.

### Not included

- **No migration of history.** A conversation stored before revisions (no
  manifest) is legacy: readable, never continued, never adopted or re-pinned.
  There is no conversion that makes old history continuable, and no messages,
  executions, sessions, checkpoints, queues, approvals, credentials or workspace
  files move into another conversation. The only supported path is a
  **reference-only bootstrap**: a new empty conversation plus a reference to the
  old one.
  - **No automatic availability of old content.** The reference is a link for a
    person, not context for the agent, the model or the tools. Pasted text
    becomes part of a new request; it is not executable state.
  - **No permission change.** The new conversation runs with the ordinary
    current permissions of its configuration and action policy; the reference
    record and its confirmation grant nothing.
- **No in-place re-pin.** Editing or copying a manifest does not make a
  conversation continuable; a changed manifest fails as tampered and one from
  another conversation fails as a context mismatch.
- **No replay and no exactly-once.** The manifest describes compatibility of a
  stored conversation only. It does not replay a turn, restore Python process
  state or an in-flight request, and it does not make external operations happen
  exactly once.
- No automatic activation, image publication or restart in this revision.

### Notes

- Conversations pinned by another Grid version, another Python minor series or
  another resolved system configuration are refused with a `revision.*` code;
  restoring the previous configuration and restarting makes them compatible
  again.
- A resumed or branched conversation is judged by the stored manifest, never by
  a manifest held only in memory.
- `tests/test_revision_compatibility.py` covers pinning, refusal codes,
  tampering, legacy read-only behaviour, secret-free storage, the gate position
  and the release manifest validator.
- `tests/test_context_migration.py` covers the migration dry run and its
  no-write behaviour, token binding, confirmation and staleness, one empty pinned
  target with the original preserved, the excluded
  approvals/queues/sessions/credentials state, idempotence, tampered, future,
  malformed and foreign sources, `target_incompatible` without re-pinning, and
  the owner-scoped API. `tests/web_chat_reference.test.mjs` covers the UI path.

## [0.1.0] - baseline

Baseline release before revision pinning existed. Conversations stored by 0.1.0
carry no manifest; under 0.2.0 and later they are legacy: readable, not
continuable, never re-pinned.
