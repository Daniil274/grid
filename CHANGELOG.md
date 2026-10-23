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
  oversized files are fingerprinted by size. Rotating a credential does not
  invalidate a conversation; changing a prompt does.

### Not included

- **No migration.** A conversation stored before revisions (no manifest) is
  legacy: readable, never continued, never adopted or re-pinned. There is no
  supported conversion; start a new conversation.
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

## [0.1.0] - baseline

Baseline release before revision pinning existed. Conversations stored by 0.1.0
carry no manifest; under 0.2.0 and later they are legacy: readable, not
continuable, never re-pinned.
